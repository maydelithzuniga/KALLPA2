"""Motor de conversación compartido por el chat web y WhatsApp.

audio → transcripción → borrador → aclaración (una pregunta por turno) → confirmación → registro.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ledger, metrics, nlu, reconcile, setup, summaries
from . import models as M
from .money import soles
from .textutil import pluralize
from . import units
from .timeutil import add_months, month_label, month_start, now_lima, today_lima

NUM_WORDS = ["cero", "un", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once",
             "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte"]

MOVEMENT_LABEL = {
    M.SALE: "venta", M.PURCHASE: "compra", M.EXPENSE: "gasto", M.WITHDRAWAL: "retiro para la casa",
    M.CONTRIBUTION: "aporte propio", M.TRANSFER: "traslado", M.COLLECTION: "cobro de fiado",
    M.DEBT_PAYMENT: "pago de cuota", M.OPENING: "saldo inicial", M.STOCK_ADJ: "ajuste de stock",
    M.GOAL_SAVING: "ahorro para meta", M.LOSS: "merma",
}


def qty_name(qty: int | None, name: str) -> str:
    name = name.lower()
    if not qty:
        return name
    if qty == 1:
        art = "una" if name.split()[0].endswith("a") else "un"
        return f"{art} {name}"
    if not float(qty).is_integer():   # «0.5 tomate» → «½ tomate»
        from . import units
        return f"{units.fmt_qty(qty)} {name}"
    qty = int(qty)
    q = NUM_WORDS[qty] if qty <= 20 else str(qty)
    return f"{q} {pluralize(name)}"


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def method_txt(m: str | None) -> str:
    return {"yape": "por Yape", "efectivo": "en efectivo"}.get(m or "", "")


def _m(method: str | None) -> str:
    t = method_txt(method)
    return f" {t}" if t else ""


class Engine:
    def __init__(self, db: Session, business: M.Business, conversation: str, channel: str = "web"):
        self.db = db
        self.b = business
        self.conv = conversation
        self.channel = channel
        self.today = today_lima()

    # ------------------------------------------------------------ utilidades
    def products(self) -> list[nlu.ProductRef]:
        return [nlu.ProductRef(p.id, p.name, p.aliases or [], p.price_cents, p.cost_cents, p.track_stock,
                               p.is_input, p.is_service)
                for p in self.db.scalars(select(M.Product).where(M.Product.business_id == self.b.id,
                                                                 M.Product.active.is_(True))).all()]

    def open_draft(self) -> M.Draft | None:
        return self.db.scalar(select(M.Draft).where(M.Draft.business_id == self.b.id, M.Draft.conversation == self.conv,
                                                    M.Draft.status == "abierto").order_by(M.Draft.id.desc()))

    def new_draft(self, action: str, data: dict[str, Any]) -> M.Draft:
        for d in self.db.scalars(select(M.Draft).where(M.Draft.business_id == self.b.id, M.Draft.conversation == self.conv,
                                                       M.Draft.status == "abierto")).all():
            d.status = "cancelado"
        d = M.Draft(business_id=self.b.id, conversation=self.conv, op_id=uuid.uuid4().hex, action=action, data=data)
        self.db.add(d)
        self.db.flush()
        return d

    def resp(self, reply: str, draft: M.Draft | None = None, quick: list[str] | None = None,
             movement: M.Movement | None = None, **extra) -> dict[str, Any]:
        out = {"reply": reply, "quick_replies": quick or [], "draft": self.draft_view(draft) if draft else None,
               "movement": movement_view(self.db, movement) if movement else None,
               "updated_at": now_lima().isoformat()}
        out.update(extra)
        return out

    def draft_view(self, d: M.Draft) -> dict[str, Any]:
        return {"op_id": d.op_id, "action": d.action, "missing": d.missing, "options": d.options,
                "data": d.data, "status": d.status}

    # ------------------------------------------------------------ entrada
    def handle(self, text: str, *, op_id: str | None = None, occurred_at: datetime | None = None,
               agent_data: dict[str, Any] | None = None) -> dict[str, Any]:
        text = (text or "").strip()
        if self.b.setup_state != "listo":
            from . import onboarding   # primera conversación: conocer el negocio y armar su inventario
            return onboarding.handle(self, text)
        parsed = nlu.parse(text, self.products(), self.today)
        draft = self.open_draft()

        if op_id:
            d_any = self.db.scalar(select(M.Draft).where(M.Draft.op_id == op_id))
            if d_any and d_any.status == "confirmado":
                mv = ledger.find_movement_by_op(self.db, op_id)
                return self.resp("Eso ya está guardado, no lo dupliqué.", movement=mv, duplicate=True)
            if d_any and d_any.status == "cancelado":
                return self.resp("Ese registro ya fue cancelado.")
            if d_any:
                draft = d_any

        if parsed.intent == "consentir_informe" or (
                re.match(r"^(si )?(envialo|mandalo|enviale|mandale)\b", nlu.normalize(text)) and self.pending_consent_month()):
            return self.consent(True, draft if draft and draft.action == "consentimiento" else None)
        if draft and draft.action == "consentimiento":
            if parsed.intent == "confirmar":
                return self.consent(True, draft)
            if parsed.intent == "cancelar" or nlu.normalize(text).startswith("no por ahora"):
                return self.consent(False, draft)
            draft.status = "cancelado"   # siguió con otra cosa: no insistimos este mes
            self.db.flush()
            draft = None
        if draft and draft.action == "propuesta_meta":
            from . import goals_coach
            r = goals_coach.reply(self, draft, text, parsed.intent)
            if r is not None:
                return r
            if parsed.intent.startswith(("resumen", "explica", "caja", "fiados", "inventario", "consejos", "reponer", "ayuda")):
                r = self.answer_question(parsed.intent, parsed.text)
                r["reply"] += "\n\n🎯 ¿Y con qué meta empiezas? Responde 1, 2 o 3."
                r["draft"] = self.draft_view(draft)
                return r
            draft.status = "cancelado"   # siguió con otra cosa (una venta…): la meta se le recuerda en la noche
            self.db.flush()
            draft = None
        if draft:
            r = self.continue_draft(draft, parsed, text, occurred_at)
            if r is not None:
                return self.with_consent_prompt(r)
        if agent_data:   # lo entendió el agente de IA (Gemini): pasa por el mismo motor
            r = self.from_agent(agent_data, text, occurred_at)
            if r is not None:
                return self.with_consent_prompt(r)
        return self.with_consent_prompt(self.fresh(parsed, text, occurred_at))

    # ------------------------------------------------------------ agente de IA (Women2)
    def _match_product(self, name: str | None) -> list[int]:
        if not name:
            return []
        pm = nlu.match_products(nlu.normalize(name), self.products())
        return [c.id for c in pm.candidates]

    def from_agent(self, ad: dict[str, Any], raw: str, occurred_at: datetime | None) -> dict[str, Any] | None:
        """Convierte la intención del agente en un borrador de Kallpa (valida, pregunta lo que falta y guarda)."""
        it = ad.get("intent") or {}
        tipo = (it.get("tipo") or "").lower()
        cents = lambda v: int(round(float(v) * 100)) if v not in (None, "", 0, 0.0) else None
        method = (it.get("metodo_pago") or "").lower() or None
        method = method if method in M.POCKETS else None
        when = self._when({}, occurred_at)
        base = {"raw_text": raw, "occurred_at": when, **({"method": method} if method else {})}
        unit_map = {"kg": "kg", "kilo": "kg", "kilos": "kg", "unidad": "unidad", "unidades": "unidad", "jaba": "jaba", "saco": "saco",
                    "atado": "atado", "caja": "caja", "bolsa": "bolsa", "docena": "docena", "soles": None}
        if tipo == "venta":
            items_in = it.get("items") or []
            total = cents(it.get("monto"))
            if it.get("fiado") or it.get("persona"):
                # venta al fiado: una sola venta con quién debe
                pc = self._match_product((items_in[0] or {}).get("producto")) if items_in else []
                data = {**base, "kind": M.SALE, "product_candidates": pc, "total_cents": total or sum(cents(x.get("monto")) or 0 for x in items_in) or None,
                        "quantity": int(items_in[0]["cantidad"]) if items_in and items_in[0].get("cantidad") and float(items_in[0]["cantidad"]).is_integer() and unit_map.get(items_in[0].get("unidad")) in (None, "unidad") else None,
                        "counterparty": it.get("persona"), "fully_credit": bool(it.get("fiado")) and not method}
                if not pc and items_in and items_in[0].get("producto"):
                    data["new_product"] = items_in[0]["producto"]
                return self.step(self.new_draft("movimiento", data))
            items = []
            for x in items_in:
                pc = self._match_product(x.get("producto"))
                unit = unit_map.get((x.get("unidad") or "").lower(), (x.get("unidad") or None))
                prod = self.db.get(M.Product, pc[0]) if len(pc) == 1 else None
                items.append({"product_id": prod.id if prod else None, "name": prod.name if prod else (x.get("producto") or "venta").capitalize(),
                              "new": prod is None and bool(x.get("producto")), "amount_cents": cents(x.get("monto")),
                              "measure": float(x["cantidad"]) if x.get("cantidad") and unit else (float(x["cantidad"]) if x.get("cantidad") else None),
                              "unit": unit or ("unidad" if x.get("cantidad") else None)})
            if not items:
                if not total:
                    return None
                data = {**base, "kind": "ventas", "lump": True, "group_total_cents": total, "items": []}
            elif total and len(items) > 1 and not any(i["amount_cents"] for i in items):
                data = {**base, "kind": "ventas", "group_total_cents": total, "items": items}
            else:
                if len(items) == 1 and items[0]["amount_cents"] is None and total:
                    items[0]["amount_cents"] = total
                data = {**base, "kind": "ventas", "items": items}
            return self.step(self.new_draft("ventas", data))
        if tipo == "compra":
            x = (it.get("items") or [{}])[0]
            pc = self._match_product(x.get("producto"))
            unit = unit_map.get((x.get("unidad") or "").lower())
            data = {**base, "kind": M.PURCHASE, "product_candidates": pc, "total_cents": cents(it.get("monto")) or cents(x.get("monto"))}
            if x.get("cantidad"):
                if unit and unit != "unidad":
                    data.update({"measure": float(x["cantidad"]), "measure_unit": unit})
                else:
                    data["quantity"] = int(float(x["cantidad"]))
            if not pc:
                data["generic"] = True
            return self.step(self.new_draft("movimiento", data))
        if tipo in ("gasto", "retiro"):
            desc = it.get("descripcion") or raw
            data = {**base, "kind": M.EXPENSE if tipo == "gasto" else M.WITHDRAWAL, "amount_cents": cents(it.get("monto")), "description": desc}
            if tipo == "gasto":
                cat, typ = "Otros gastos", "variable"
                for pat, c, ty in nlu.EXPENSE_CATEGORIES:
                    if re.search(pat, nlu.normalize(desc)):
                        cat, typ = c, ty
                        break
                data.update({"category": cat, "expense_type": typ})
            return self.step(self.new_draft("movimiento", data))
        if tipo == "perdida":
            lf = nlu.loss_fields(nlu.normalize(raw), self.products())
            x = (it.get("items") or [{}])[0]
            if x.get("producto") and not lf.get("product_candidates"):
                lf["product_candidates"] = self._match_product(x["producto"])
            if cents(it.get("monto")) and not lf.get("amount_cents"):
                lf["amount_cents"] = cents(it.get("monto"))
            if method and lf.get("loss_kind") == "plata":
                lf["method"] = method
            if not lf.get("what") and it.get("descripcion"):
                lf["what"] = it["descripcion"]
            return self.step(self.new_draft("perdida", {**lf, "raw_text": raw, "occurred_at": when}))
        if tipo == "cobro_fiado":
            data = {**base, "kind": M.COLLECTION, "amount_cents": cents(it.get("monto")), "counterparty": it.get("persona")}
            return self.step(self.new_draft("movimiento", data))
        if tipo == "meta" and cents(it.get("monto")):
            data = {**base, "kind": M.GOAL_SAVING, "amount_cents": cents(it.get("monto")), "goal_hint": nlu.normalize(it.get("nombre_meta") or "")}
            return self.step(self.new_draft("movimiento", data))
        if tipo == "cierre":
            return self.start_close()
        if tipo == "consulta" and ad.get("respuesta_voz"):
            return self.resp(ad["respuesta_voz"])
        return None

    def fresh(self, p: nlu.Parsed, raw: str, occurred_at: datetime | None) -> dict[str, Any]:
        i = p.intent
        if i == "confirmar":
            return self.resp("No tengo nada pendiente por guardar. Cuéntame qué vendiste o gastaste.")
        if i == "cancelar":
            return self.resp("Ok, no hay nada pendiente.")
        if i == "movimiento":
            data = dict(p.fields)
            data["raw_text"] = raw
            data["occurred_at"] = self._when(data, occurred_at)
            d = self.new_draft("ventas" if data.get("kind") == "ventas" else "movimiento", data)
            return self.step(d)
        if i == "deshacer":
            return self.undo_last()
        if i == "cobrar":
            return self.collect_message(p)
        if i == "contar_plata":
            return self.start_close(count_only=True)
        if i == "perdida":
            d = self.new_draft("perdida", {**p.fields, "raw_text": raw, "occurred_at": self._when(p.fields, occurred_at)})
            return self.step(d)
        if i == "cierre":
            return self.start_close()
        if i == "catalogo":
            d = self.new_draft("catalogo", dict(p.fields))
            return self.step(d)
        if i == "saldo_inicial":
            bal = p.fields.get("balances") or {}
            if not bal:
                return self.resp("¿Con cuánto empiezas en efectivo y en Yape?")
            d = self.new_draft("saldo", {"balances": bal})
            return self.step(d)
        if i == "conteo_stock":
            d = self.new_draft("conteo", {"kind": "stock", **p.fields})
            return self.step(d)
        if i == "conteo_caja":
            c = reconcile.cash_count(self.db, self.b, p.fields["pocket"], p.fields["counted_cents"])
            return self.resp(reconcile.count_text(self.db, c), count=metrics._count_dict(c))
        if i == "meta":
            if not p.fields.get("target_cents"):   # «quiero una meta» sin monto: le pregunto de cuánto y le propongo con sus números
                from . import finance_coach, goals_coach
                if finance_coach.wants_advice(raw):   # «consejos para llegar a mi meta»: plan de la meta que ya tiene
                    try:
                        plan = finance_coach.goal_advice(self.db, self.b, self.today)
                    except Exception:   # noqa: BLE001
                        plan = None
                    if plan:
                        return self.resp(plan)
                txt, d, q = goals_coach.start(self, ask_amount=True)
                if txt:
                    self.db.commit()
                    return self.resp(txt, d, q)
            fields = dict(p.fields)
            if not fields.get("name") and fields.get("kind") != "reposicion":
                fields["ask_purpose"] = True   # con monto pero sin «para qué»: se lo pregunto
            d = self.new_draft("meta", fields)
            return self.step(d)
        if i == "ahorro_meta":
            d = self.new_draft("movimiento", {"kind": M.GOAL_SAVING, **p.fields, "raw_text": raw,
                                              "occurred_at": self._when(p.fields, occurred_at)})
            return self.step(d)
        if i == "corregir":
            return self.start_correction(p)
        if i == "anular":
            return self.undo_last()
        if i == "gasto_fijo":
            from . import costs
            saved = []
            for it in p.fields["items"]:
                fx = self.db.scalar(select(M.FixedExpense).where(M.FixedExpense.business_id == self.b.id,
                                                                  M.FixedExpense.name == it["name"]))
                fx = fx or M.FixedExpense(business_id=self.b.id, name=it["name"])
                fx.amount_cents = it["amount_cents"]
                fx.frequency = it["frequency"] or "mensual"
                fx.cost_type = it["cost_type"]
                fx.due_day = it.get("due_day") or fx.due_day or 1
                self.db.add(fx)
                saved.append(costs.line({**it, "frequency": fx.frequency}))
            self.db.commit()
            be = summaries.explain(self.db, self.b, "equilibrio").replace(
                "Es lo mínimo que tienes que vender para no perder: lo que cubre tus gastos fijos del mes. ", "")
            return self.resp("✓ Anoté tu gasto: " + "; ".join(saved) + ". " + be)
        if i == "consentir_informe":
            return self.consent(True)
        return self.answer_question(i, raw)

    def _when(self, fields: dict[str, Any], occurred_at: datetime | None) -> str:
        if fields.get("day"):
            d = date.fromisoformat(fields["day"])
            base = occurred_at or now_lima()
            return datetime(d.year, d.month, d.day, base.hour, base.minute).isoformat()
        return (occurred_at or now_lima()).isoformat()

    # ------------------------------------------------------------ preguntas
    def answer_question(self, intent: str, text: str = "") -> dict[str, Any]:
        db, b = self.db, self.b
        if intent == "resumen_dia":
            return self.resp(summaries.daily(db, b))
        if intent == "resumen_semana":
            return self.resp(summaries.weekly(db, b))
        if intent == "resumen_mes":
            return self.resp(summaries.monthly(db, b))
        if intent == "caja":
            return self.resp(summaries.cash_line(db, b))
        if intent == "reponer":
            gs = [g for g in metrics.goals(db, b) if g["kind"] == "reposicion" and g["status"] == "activa"]
            if gs:
                return self.resp(gs[0]["message"])
            cash = metrics.cash_position(db, b)
            if cash["available_for_restock_cents"] is None:
                return self.resp(cash["missing_text"])
            return self.resp(f"Según tu caja y tus pagos registrados, hoy puedes usar {soles(max(0, cash['available_for_restock_cents']))} para reponer. "
                             "Dime «quiero reponer mercadería por 200» y lo sigo como meta.")
        if intent == "consejos":
            tips = metrics.advice(db, b)
            from . import finance_coach   # su meta con plan (o la invitación a ponerse una)
            return self.resp(" ".join(tips[:2]) + finance_coach.consejos_footer(db, b, self.today))
        if intent == "consejos_financieros":
            from . import finance_coach
            try:
                return self.resp(finance_coach.consult(db, b, text, self.today))
            except Exception:   # noqa: BLE001
                return self.resp(" ".join(metrics.advice(db, b)[:2]))
        if intent.startswith("explica_"):
            return self.resp(summaries.explain(db, b, intent.split("_", 1)[1]))
        if intent == "fiados":
            return self.collect_message(nlu.Parsed(intent="cobrar"))
        if intent == "metas":
            if not [g for g in metrics.goals(db, b) if g["status"] == "activa"]:
                from . import goals_coach
                txt, d, q = goals_coach.start(self)
                if txt:
                    self.db.commit()
                    return self.resp(txt, d, q)
            return self.resp(summaries.goals_text(db, b) + " Escribe «consejos» y te digo cómo llegar más rápido y qué ahorro o préstamo te ayuda.")
        if intent == "inventario":
            return self.resp(summaries.inventory_text(db, b, text))
        if intent == "informe":
            from .config import settings
            from .report_pdf import analyze
            try:
                a = analyze(db, b)
                resumen = (f"Ingreso mensual promedio verificado: {soles(a['capacity']['avg_income_cents'] or 0)}. "
                           f"Flujo neto: {soles(a['capacity']['avg_net_cents'] or 0)} al mes. "
                           f"Cuota disponible para un crédito: {soles(a['available_installment_cents'])}. "
                           f"Resultado: {a['profile'].lower()}.")
            except Exception:   # noqa: BLE001 (sin movimientos por Yape aún)
                resumen = "Todavía no tengo suficientes movimientos por Yape verificados para un informe completo."
            url = f"{settings.app_base_url}/api/businesses/{b.id}/report/pdf" if settings.app_base_url else None
            return self.resp("📄 Tu informe financiero para el banco (solo lo verificado por Yape). " + resumen
                             + ("" if url else " Lo descargas en la app, en «Informe BCP»."), media_url=url)
        if intent == "ayuda":
            name = f" {self.b.owner_name}" if self.b.owner_name else ""
            return self.resp(f"¡Hola{name}! " + summaries.help_text(db, b))
        return self.resp("No te entendí bien. " + summaries.help_text(db, b), quick=["Resumen de hoy", "¿Cuánto tengo?"])

    # ------------------------------------------------------------ borradores
    def continue_draft(self, d: M.Draft, p: nlu.Parsed, raw: str, occurred_at) -> dict[str, Any] | None:
        if d.action == "cierre":
            return self.close_step(d, p, raw)
        if p.intent == "deshacer":
            d.status = "cancelado"
            self.db.commit()
            return self.resp("Listo, no lo guardé.")
        if p.intent == "confirmar":
            return self.confirm(d)
        if p.intent == "cancelar":
            d.status = "cancelado"
            self.db.commit()
            return self.resp("Listo, no lo guardé.")
        new_movement = p.intent in ("movimiento", "catalogo", "meta", "saldo_inicial", "conteo_stock", "conteo_caja",
                                    "corregir", "anular", "ahorro_meta", "cierre", "perdida") and not raw.lower().startswith("no")
        if new_movement and d.missing and not self._substantive(p):
            new_movement = False
        if new_movement and not (d.missing and p.intent == "movimiento" and self._answers_missing(d, p)):
            d.status = "cancelado"
            self.db.flush()
            return None
        if p.intent.startswith(("resumen", "explica", "caja", "fiados", "metas", "inventario", "consejos", "reponer", "ayuda")) \
                and p.intent != "valor":
            r = self.answer_question(p.intent, p.text)
            r["reply"] += " (Sigo esperando tu respuesta sobre lo anterior.)"
            r["draft"] = self.draft_view(d)
            return r
        changed = self.apply_answer(d, p, raw)
        if not changed:
            q = self.step(d, reask=True)
            return q
        if d.data.get("paid_cents") == 0 and d.missing == "method":
            return self.step(d)
        # Si ya se leyó el resumen y solo faltaba el medio de pago, la respuesta completa y confirma
        auto = d.missing == "method" and d.summary_read and d.data.get("method") in M.POCKETS
        res = self.step(d, auto_confirm=auto)
        return res

    @staticmethod
    def _substantive(p: nlu.Parsed) -> bool:
        f = p.fields
        keys = ("product_candidates", "new_product", "total_cents", "amount_cents", "ambiguous_price_cents",
                "unit_price_cents", "ambiguous_cents", "price_cents", "cost_cents", "counted", "counted_cents",
                "target_cents", "balances", "stock", "items", "group_total_cents", "lump")
        return any(f.get(k) for k in keys)

    def _answers_missing(self, d: M.Draft, p: nlu.Parsed) -> bool:
        return d.missing in ("method",) and "method" in p.fields and len(p.text.split()) <= 4

    def apply_answer(self, d: M.Draft, p: nlu.Parsed, raw: str) -> bool:
        data = dict(d.data)
        f = p.fields
        changed = False
        miss = d.missing
        if miss == "loss_what" and raw.strip():
            data["what"] = nlu.normalize(raw)
            lf = nlu.loss_fields(nlu.normalize(raw), self.products())
            for k2 in ("product_candidates", "measure", "measure_unit", "amount_cents"):
                if lf.get(k2):
                    data[k2] = lf[k2]
            changed = True
        if miss == "loss_value":
            got = self._loss_answer(p)
            if got:
                data.update(got)
                changed = True
        if miss and miss.startswith("item:"):
            amt = f.get("total_cents") or f.get("amount_cents") or f.get("bare_number") or f.get("unit_price_cents") \
                or f.get("ambiguous_price_cents")
            if amt:
                idx = int(miss.split(":")[1])
                items = [dict(i) for i in data["items"]]
                items[idx]["amount_cents"] = int(amt)
                data["items"] = items
                changed = True
        if miss == "product" and d.options:
            choice = None
            if f.get("option_index") and 1 <= f["option_index"] <= len(d.options):
                choice = d.options[f["option_index"] - 1]
            else:
                t = nlu.normalize(raw)
                for opt in d.options:
                    words = [w for w in nlu.normalize(opt["name"]).split() if len(w) > 2]
                    if any(w in t.split() for w in words[1:] or words):
                        choice = opt
                        break
                if not choice and f.get("product_candidates"):
                    ids = [o["id"] for o in d.options]
                    inter = [c for c in f["product_candidates"] if c in ids]
                    if len(inter) == 1:
                        choice = next(o for o in d.options if o["id"] == inter[0])
            if choice:
                data["product_candidates"] = [choice["id"]]
                changed = True
        elif miss == "price_role":
            role = f.get("price_role")
            t = nlu.normalize(raw)
            if not role:
                if "total" in t or "todo" in t:
                    role = "total"
                elif "cada" in t or "unidad" in t or "uno" in t:
                    role = "unidad"
            if role:
                amb = data.pop("ambiguous_price_cents")
                if role == "total":
                    data["total_cents"] = amb
                else:
                    data["unit_price_cents"] = amb
                changed = True
        elif miss == "catalog_role":
            role = f.get("catalog_role")
            t = nlu.normalize(raw)
            if not role:
                if any(w in t for w in ("vendo", "cobro", "venta", "precio")):
                    role = "precio"
                elif any(w in t for w in ("compro", "cuesta", "costo", "pago", "compra")):
                    role = "costo"
            if role:
                amb = data.pop("ambiguous_cents")
                data["price_cents" if role == "precio" else "cost_cents"] = amb
                changed = True
        elif miss == "counterparty":
            clean = raw.strip(" .,!¿?").strip()
            name = clean.title() if 0 < len(clean.split()) <= 3 and not any(ch.isdigit() for ch in clean) else f.get("counterparty")
            if not name:
                words = [w for w in nlu.normalize(raw).split() if w not in nlu.NAME_STOP and not w.isdigit()]
                name = words[0].capitalize() if words else None
            if nlu.normalize(raw) in ("nadie", "no se", "no importa", "ninguno", "sin nombre", "cliente"):
                name = "Sin nombre"
            if name:
                data["counterparty"] = name
                changed = True
        elif miss == "parent":
            name = f.get("counterparty")
            if f.get("option_index") and d.options and 1 <= f["option_index"] <= len(d.options):
                data["parent_id"] = d.options[f["option_index"] - 1]["id"]
                changed = True
            elif d.options:
                t = nlu.normalize(raw)
                for opt in d.options:
                    if nlu.normalize(opt["name"]).split()[0] in t.split() or (name and name.lower() == opt["name"].lower()):
                        data["parent_id"] = opt["id"]
                        changed = True
                        break
        elif miss == "goal" and d.options:
            t = nlu.normalize(raw)
            for i, opt in enumerate(d.options, 1):
                if f.get("option_index") == i or any(w in t.split() for w in nlu.normalize(opt["name"]).split() if len(w) > 3):
                    data["goal_id"] = opt["id"]
                    changed = True
                    break
        elif miss == "amount_choice":
            amt = f.get("amount_cents") or f.get("total_cents") or f.get("bare_number")
            if f.get("option_index") and d.options and 1 <= f["option_index"] <= len(d.options):
                amt = d.options[f["option_index"] - 1]["cents"]
            if amt:
                data.pop("amount_options", None)
                data["total_cents"] = amt
                changed = True
        if miss == "method" and data.get("kind") == M.SALE and f.get("fully_credit"):
            data["paid_cents"] = 0
            data["method"] = None
            changed = True
        # valores generales (correcciones del borrador: «no, eran dos», «fue por Yape», «50»)
        for key in ("method", "quantity", "counterparty", "paid_cents", "pending_cents"):
            if key in f and f[key] is not None and data.get(key) != f[key]:
                if key == "counterparty" and miss not in ("counterparty", None) and data.get("counterparty"):
                    continue
                data[key] = f[key]
                changed = True
        if f.get("product_candidates") and miss != "product" and d.action in ("movimiento", "catalogo", "conteo"):
            if data.get("product_candidates") != f["product_candidates"]:
                data["product_candidates"] = f["product_candidates"]
                data.pop("product_id", None)
                changed = True
        amount = f.get("total_cents") or f.get("amount_cents") or f.get("bare_number")
        if amount and miss not in ("price_role", "amount_choice", "catalog_role"):
            if d.action == "movimiento":
                if data.get("kind") in (M.SALE, M.PURCHASE):
                    if f.get("price_role") == "unidad":
                        data["unit_price_cents"] = amount
                        data.pop("total_cents", None)
                    elif not (miss == "quantity" and "quantity" in f):
                        data["total_cents"] = amount
                        data.pop("unit_price_cents", None)
                        data.pop("ambiguous_price_cents", None)
                else:
                    data["amount_cents"] = amount
                changed = True
            elif d.action == "meta" and miss != "name":   # al contestar «¿para qué es?» («una congeladora») no se cambia el monto
                data["target_cents"] = amount
                changed = True
            elif d.action == "catalogo" and miss == "catalog_value":
                data["ambiguous_cents"] = amount
                changed = True
        if miss == "quantity" and f.get("bare_number") and "quantity" not in f:
            data["quantity"] = f["bare_number"] // 100
            data.pop("total_cents", None) if data.get("total_cents") == f["bare_number"] else None
            changed = True
        if miss == "name" and d.action == "meta":
            data["name"] = cap(raw.strip())
            changed = True
        if miss == "stock_qty" and (f.get("quantity") or f.get("bare_number")):
            data["counted"] = f.get("quantity") or f["bare_number"] // 100
            changed = True
        if changed:
            d.data = data
            self.db.flush()
        return changed

    def ask(self, d: M.Draft, field: str, question: str, quick: list[str] | None = None,
            options: list | None = None, summary_read: bool = False) -> dict[str, Any]:
        d.missing = field
        d.options = options
        d.summary_read = summary_read
        self.db.commit()
        return self.resp(question, d, quick)

    def step(self, d: M.Draft, reask: bool = False, auto_confirm: bool = False) -> dict[str, Any]:
        handler = {"movimiento": self.step_movement, "catalogo": self.step_catalog, "saldo": self.step_balance,
                   "conteo": self.step_count, "meta": self.step_goal, "correccion": self.step_correction,
                   "ventas": self.step_items, "perdida": self.step_loss,
                   "anulacion": lambda d, **k: self.resp("¿Lo anulo?", d, ["Sí", "No"])}[d.action]
        r = handler(d, auto_confirm=auto_confirm)
        # Se guarda al toque cuando ya no falta nada (sin pedir confirmación)
        if d.status == "abierto" and d.missing is None and d.action in AUTO_SAVE:
            return self.confirm(d)
        if reask and r.get("reply"):
            r["reply"] = "No te entendí. " + r["reply"]
        return r

    # ------------------------------------------------------------ movimientos
    def _product(self, data: dict[str, Any]) -> M.Product | None:
        if data.get("product_id"):
            return self.db.get(M.Product, data["product_id"])
        c = data.get("product_candidates") or []
        if len(c) == 1:
            data["product_id"] = c[0]
            return self.db.get(M.Product, c[0])
        return None

    def step_movement(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        data = dict(d.data)
        k = data["kind"]
        product = None
        if k in (M.SALE, M.PURCHASE, M.STOCK_ADJ):
            cands = data.get("product_candidates") or []
            if len(cands) > 1 and not data.get("product_id"):
                opts = [{"id": p.id, "name": p.name} for p in (self.db.get(M.Product, c) for c in cands)]
                names = " o ".join(o["name"].lower() for o in opts)
                d.data = data
                return self.ask(d, "product", f"¿Cuál fue: {names}?", [o["name"] for o in opts], opts)
            product = self._product(data)
            if product is None and data.get("new_product"):
                data["new_product_name"] = cap(data["new_product"])
            if k == M.STOCK_ADJ and product is None:
                d.data = data
                return self.ask(d, "product", "¿De qué producto?",
                                [p.name for p in self.products()[:4]], [{"id": p.id, "name": p.name} for p in self.products()])

        if k in (M.SALE, M.PURCHASE):
            qty = data.get("quantity")
            ref_unit = (product.price_cents if k == M.SALE else product.cost_cents) if product else None
            if k == M.PURCHASE and data.get("measure") and product:
                qty = data["measure"]
                c = (product.spec or {}).get("compra_por") or {}
                if c.get("unidad") == data.get("measure_unit") and c.get("costo_cents"):
                    ref_unit = c["costo_cents"]
                else:
                    eq = units.equivalence(product, data.get("measure_unit"))
                    ref_unit = round(product.cost_cents * eq) if product.cost_cents and eq else None
            if data.get("amount_options") and len(set(data["amount_options"])) > 1 and not data.get("total_cents_confirmed"):
                opts = [{"cents": c} for c in data["amount_options"]]
                d.data = data
                return self.ask(d, "amount_choice",
                                f"Escuché dos montos: {' y '.join(soles(c) for c in data['amount_options'])}. ¿Cuál fue el total?",
                                [soles(c) for c in data["amount_options"]], opts)
            if data.get("ambiguous_price_cents") is not None:
                amb = data["ambiguous_price_cents"]
                if not qty or qty == 1:
                    data["total_cents"] = data.pop("ambiguous_price_cents")
                elif ref_unit and abs(amb - ref_unit * qty) <= 0.25 * ref_unit * qty:
                    data["total_cents"] = data.pop("ambiguous_price_cents")   # «2 polos a 40» con polo a 20: es el total
                elif ref_unit and abs(amb - ref_unit) <= 0.25 * ref_unit and abs(amb - ref_unit * qty) > 0.25 * ref_unit * qty:
                    data["unit_price_cents"] = data.pop("ambiguous_price_cents")
                else:
                    d.data = data
                    return self.ask(d, "price_role", f"¿{soles(amb)} es el total o el precio de cada uno?",
                                    ["Es el total", "Cada uno"])
            if data.get("unit_price_cents") is not None and qty:
                data["total_cents"] = round(data.pop("unit_price_cents") * qty)
            if data.get("total_cents") is None:
                if data.get("paid_cents") is not None and data.get("pending_cents") is not None:
                    data["total_cents"] = data["paid_cents"] + data["pending_cents"]
                elif k == M.SALE and data.get("fully_credit") and data.get("pending_cents"):
                    data["total_cents"] = data["pending_cents"]
                elif k == M.SALE and ref_unit and qty:
                    # No dijo el monto pero sabemos su precio: se guarda al toque con el precio de siempre
                    # (si cobró distinto, dice «corrige» o «deshacer»).
                    data["total_cents"] = round(ref_unit * qty)
                    data["price_from_list"] = True
                else:
                    d.data = data
                    hint = []
                    if ref_unit and qty:
                        hint = [soles(ref_unit * qty)]
                    return self.ask(d, "amount", "¿Por cuánto en total?" if k == M.SALE else "¿Cuánto pagaste en total?", hint)
            if k == M.PURCHASE and not qty and not data.get("measure") and product and product.track_stock and not data.get("generic"):
                d.data = data
                return self.ask(d, "quantity", f"¿Cuántas unidades de {product.name.lower()} compraste?")
            if k == M.PURCHASE and (data.get("generic") or not product):
                data["units_pending"] = True
            total = data["total_cents"]
            # pagos parciales y fiados
            if k == M.SALE:
                if data.get("fully_credit") and data.get("paid_cents") is None:
                    data["paid_cents"] = 0
                if data.get("pending_cents") is not None and data.get("paid_cents") is None:
                    data["paid_cents"] = max(0, total - data["pending_cents"])
                paid = data.get("paid_cents", total)
                if paid > total:
                    data["paid_cents"] = total
                    paid = total
                if paid < total and not data.get("counterparty"):
                    d.data = data
                    return self.ask(d, "counterparty", "¿Quién te quedó debiendo?")
            else:
                paid = total
            if paid > 0 and data.get("method") not in M.POCKETS:
                d.data = data
                summary = self.summary_text(data, product, with_question=False)
                q = "¿En efectivo o por Yape?" if data.get("method") != "mixto" else \
                    "¿Fue en efectivo o por Yape? Si fue parte y parte, dímelo como dos registros."
                return self.ask(d, "method", f"{summary} {q}", ["Efectivo", "Yape"], summary_read=True)
        elif k == M.TRANSFER:
            if not data.get("amount_cents"):
                d.data = data
                return self.ask(d, "amount", "¿Cuánto pasaste?")
        elif k == M.STOCK_ADJ:
            if data.get("amount_cents") and data.get("method") not in M.POCKETS:
                d.data = data
                return self.ask(d, "method", "¿Devolviste la plata en efectivo o por Yape?", ["Efectivo", "Yape"])
        else:
            amount = data.get("amount_cents") or data.get("total_cents") or data.get("paid_cents") or data.get("bare_number")
            if k == M.COLLECTION:
                r = self._resolve_parent(d, data)
                if r:
                    return r
                if not amount:
                    sale = self.db.get(M.Movement, data["parent_id"])
                    pend = ledger.sale_pending(self.db, sale)
                    d.data = data
                    return self.ask(d, "amount", f"¿Cuánto te pagó? Te debía {soles(pend)}.", [soles(pend)])
            if k == M.DEBT_PAYMENT:
                debts = self.db.scalars(select(M.Debt).where(M.Debt.business_id == self.b.id, M.Debt.active.is_(True))).all()
                if len(debts) == 1:
                    data["debt_id"] = debts[0].id
                    data["counterparty"] = data.get("counterparty") or debts[0].creditor
                    amount = amount or debts[0].installment_cents
            if k == M.GOAL_SAVING:
                r = self._resolve_goal(d, data)
                if r:
                    return r
            if not amount:
                d.data = data
                return self.ask(d, "amount", "¿De cuánto fue?")
            data["amount_cents"] = amount
            if data.get("method") not in M.POCKETS:
                d.data = data
                summary = self.summary_text(data, product, with_question=False)
                verb = "¿Salió del efectivo o del Yape?" if k in (M.EXPENSE, M.WITHDRAWAL, M.DEBT_PAYMENT, M.GOAL_SAVING) \
                    else "¿Entró en efectivo o por Yape?"
                return self.ask(d, "method", f"{summary} {verb}", ["Efectivo", "Yape"], summary_read=True)
        d.data = data
        if auto_confirm:
            return self.confirm(d)
        d.missing = None
        d.options = None
        d.summary_read = True
        self.db.commit()
        return self.resp(self.summary_text(data, product), d, ["Sí, guardar", "Corregir", "Cancelar"])

    def _resolve_parent(self, d: M.Draft, data: dict[str, Any]) -> dict[str, Any] | None:
        if data.get("parent_id"):
            return None
        rec = ledger.receivables(self.db, self.b.id)
        if not rec:
            d.data = data
            d.status = "cancelado"
            self.db.commit()
            return self.resp("No tengo fiados pendientes registrados. Si fue una venta nueva, dime «vendí...».")
        who = (data.get("counterparty") or "").lower()
        mine = [r for r in rec if r["counterparty"].lower() == who] if who else []
        if mine:
            data["parent_id"] = mine[0]["sale_id"]
            data["counterparty"] = mine[0]["counterparty"]
            return None
        if len(rec) == 1:
            data["parent_id"] = rec[0]["sale_id"]
            data["counterparty"] = rec[0]["counterparty"]
            return None
        opts = [{"id": r["sale_id"], "name": r["counterparty"]} for r in rec[:5]]
        d.data = data
        return self.ask(d, "parent", "¿Quién te pagó? " + ", ".join(f"{o['name']} ({soles(r['pending_cents'])})" for o, r in zip(opts, rec)),
                        [o["name"] for o in opts], opts)

    def _resolve_goal(self, d: M.Draft, data: dict[str, Any]) -> dict[str, Any] | None:
        if data.get("goal_id"):
            return None
        gs = self.db.scalars(select(M.Goal).where(M.Goal.business_id == self.b.id, M.Goal.kind == "ahorro",
                                                  M.Goal.status == "activa")).all()
        if not gs:
            d.status = "cancelado"
            self.db.commit()
            return self.resp("Aún no tienes una meta de ahorro. Dime «quiero juntar 1500 para una congeladora».")
        hint = nlu.normalize(data.get("goal_hint") or "")
        for g in gs:
            if hint and any(w in hint.split() for w in nlu.normalize(g.name).split() if len(w) > 3):
                data["goal_id"] = g.id
                return None
        if len(gs) == 1:
            data["goal_id"] = gs[0].id
            return None
        opts = [{"id": g.id, "name": g.name} for g in gs]
        d.data = data
        return self.ask(d, "goal", "¿Para cuál meta? " + " o ".join(o["name"] for o in opts), [o["name"] for o in opts], opts)

    def summary_text(self, data: dict[str, Any], product: M.Product | None, with_question: bool = True) -> str:
        k = data["kind"]
        q = " ¿Lo guardo?" if with_question else ""
        name = product.name if product else data.get("new_product_name") or ("mercadería" if k == M.PURCHASE else "venta")
        if k == M.SALE:
            total = data["total_cents"]
            qty = data.get("quantity")
            what = qty_name(qty, name) if (product or data.get("new_product_name")) else "Venta"
            s = f"{cap(what)}, {soles(total)}"
            paid = data.get("paid_cents", total)
            if data.get("method") in M.POCKETS and paid:
                s += f" {method_txt(data['method'])}"
            if product and product.price_cents and qty:
                disc = max(0, product.price_cents * qty - total)
                if disc:
                    s += f", con {soles(disc)} de descuento"
            if paid < total:
                s += f". Te pagó {soles(paid)} y te debe {soles(total - paid)}" + (f" ({data['counterparty']})" if data.get("counterparty") else "")
            if data.get("new_product_name") and not product:
                s += ". Es un producto nuevo, lo agrego a tu catálogo"
            return s + "." + q
        if k == M.PURCHASE:
            qty = data.get("quantity")
            total = data["total_cents"]
            if qty and (product or data.get("new_product_name")):
                s = f"Compra de {qty_name(qty, name)} por {soles(total)}{_m(data.get('method'))}, a {soles(round(total / qty))} cada uno"
            elif data.get("measure") and (product or data.get("new_product_name")):
                s = f"Compra de {units.fmt_qty(data['measure'])} {units.unit_name(data.get('measure_unit'), data['measure'])} de {name.lower()} por {soles(total)}{_m(data.get('method'))}"
            else:
                s = f"Compra de mercadería por {soles(total)}{_m(data.get('method'))}. Las unidades quedan pendientes"
            return s.strip() + "." + q
        amt = data.get("amount_cents")
        if k == M.EXPENSE:
            return f"Gasto del negocio: {data.get('category', 'gasto').lower()}, {soles(amt)}{_m(data.get('method'))}.{q}"
        if k == M.WITHDRAWAL:
            desc = (data.get("description") or "").strip()
            extra = f" ({desc})" if desc and desc.lower() not in ("casa", "la casa", "para la casa") else ""
            return f"Para la casa{extra}: {soles(amt)}{_m(data.get('method'))}. No cuenta como gasto del negocio.{q}"
        if k == M.CONTRIBUTION:
            return f"Aporte de tu plata al negocio: {soles(amt)}{_m(data.get('method'))}. No cuenta como venta.{q}"
        if k == M.TRANSFER:
            return f"Paso {soles(amt)} de {data['method']} a {data['to_method']}. No cambia tus ventas.{q}"
        if k == M.COLLECTION:
            return f"{data.get('counterparty', 'Tu cliente')} te pagó {soles(amt)} del fiado{_m(data.get('method'))}.{q}"
        if k == M.DEBT_PAYMENT:
            return f"Pago de cuota{(' a ' + data['counterparty']) if data.get('counterparty') else ''}: {soles(amt)}{_m(data.get('method'))}.{q}"
        if k == M.GOAL_SAVING:
            g = self.db.get(M.Goal, data["goal_id"]) if data.get("goal_id") else None
            return f"Separas {soles(amt)}{(' del ' + data['method']) if data.get('method') else ''} para «{g.name if g else 'tu meta'}».{q}"
        if k == M.STOCK_ADJ:
            label = {"merma": "Merma", "devolucion": "Devolución", "ajuste": "Ajuste"}[data.get("reason", "ajuste")]
            return f"{label}: {qty_name(data.get('quantity'), name)}.{q}"
        return "¿Lo guardo?"

    # ------------------------------------------------------------ confirmación
    def confirm(self, d: M.Draft) -> dict[str, Any]:
        if d.status == "confirmado":
            return self.resp("Eso ya está guardado.", duplicate=True)
        if d.missing and d.action != "anulacion":
            r = self.step(d)
            if d.missing:
                return r
        handler = {"movimiento": self.confirm_movement, "catalogo": self.confirm_catalog, "saldo": self.confirm_balance,
                   "meta": self.confirm_goal, "correccion": self.confirm_correction, "anulacion": self.confirm_void,
                   "consentimiento": lambda d: self.consent(True, d), "conteo": lambda d: self.step_count(d),
                   "ventas": self.confirm_items, "perdida": self.confirm_loss}[d.action]
        return handler(d)

    def confirm_movement(self, d: M.Draft) -> dict[str, Any]:
        data = dict(d.data)
        product = self._product(data)
        if product is None and data.get("new_product_name") and data["kind"] in (M.SALE, M.PURCHASE):
            nm = data["new_product_name"]
            product = M.Product(business_id=self.b.id, name=nm, aliases=[nlu.normalize(nm), nlu.normalize(nm) + "s"],
                                track_stock=self.b.kind == "reventa", is_service=self.b.kind == "servicios")
            if data["kind"] == M.SALE and data.get("quantity"):
                product.price_cents = round(data["total_cents"] / data["quantity"])
            self.db.add(product)
            self.db.flush()
            data["product_id"] = product.id
        payload = {k: v for k, v in data.items() if k in {
            "kind", "occurred_at", "method", "to_method", "product_id", "quantity", "category", "expense_type",
            "counterparty", "description", "raw_text", "parent_id", "goal_id", "debt_id", "units_pending", "reason",
            "paid_cents", "generic", "measure", "measure_unit"}}
        payload["amount_cents"] = data.get("total_cents") if data["kind"] in (M.SALE, M.PURCHASE) else data.get("amount_cents")
        if data["kind"] == M.PURCHASE and data.get("quantity") and not product:
            payload["units_pending"] = True
        try:
            mv, created = ledger.record(self.db, self.b, payload, op_id=d.op_id, channel=self.channel, commit=False)
        except ledger.LedgerError as e:
            self.db.rollback()
            return self.resp(f"No pude guardarlo: {e}.")
        d.status = "confirmado"
        d.data = {**d.data, "movement_ids": [mv.id]}
        self.db.commit()
        return self.resp(self.after_confirm(mv), movement=mv, quick=["Deshacer"] if created else [])

    def after_confirm(self, mv: M.Movement) -> str:
        return "✓ " + short_line(self.db, mv, with_method=True)

    # ------------------------------------------------------------ varias ventas en un mensaje
    def step_items(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        data = dict(d.data)
        items = [dict(i) for i in data.get("items", [])]
        if not data.get("group_total_cents"):
            for idx, it in enumerate(items):
                if it.get("amount_cents") is None:
                    prod = self.db.get(M.Product, it["product_id"]) if it.get("product_id") else None
                    price = units.price_for(prod, it.get("measure"), it.get("unit")) if prod and it.get("measure") else None
                    if price:
                        it["amount_cents"] = price
                        it["price_from_catalog"] = True
                    else:
                        data["items"] = items
                        d.data = data
                        q = f"¿Cuánto cobraste por {measure_txt(it.get('measure'), it.get('unit'))} de {it['name'].lower()}?" \
                            if it.get("measure") else f"¿Cuánto cobraste por el {it['name'].lower()}?"
                        return self.ask(d, f"item:{idx}", q)
        data["items"] = items
        if data.get("method") not in M.POCKETS:
            d.data = data
            q = "¿Te pagaron en efectivo o por Yape?" if data.get("method") != "mixto" else \
                "¿Fue en efectivo o por Yape? Si fue parte y parte, dímelo en dos mensajes."
            return self.ask(d, "method", q, ["Efectivo", "Yape"])
        d.data = data
        d.missing = None
        self.db.flush()
        return self.resp("", d)

    def confirm_items(self, d: M.Draft) -> dict[str, Any]:
        data = dict(d.data)
        when = data.get("occurred_at")
        mvs: list[M.Movement] = []
        try:
            if data.get("group_total_cents"):
                names = [i["name"].lower() for i in data.get("items", [])]
                desc = (", ".join(names[:-1]) + " y " + names[-1]) if len(names) > 1 else (names[0] if names else "ventas varias")
                mv, _ = ledger.record(self.db, self.b, {"kind": M.SALE, "amount_cents": data["group_total_cents"], "method": data["method"],
                                                        "description": desc, "raw_text": data.get("raw_text"), "occurred_at": when,
                                                        "category": "Ventas varias" if data.get("lump") else "Venta de productos"},
                                      op_id=d.op_id, channel=self.channel, commit=False)
                mvs.append(mv)
            else:
                for idx, it in enumerate(data["items"]):
                    pid = it.get("product_id")
                    if not pid:
                        prod = M.Product(business_id=self.b.id, name=it["name"], aliases=[nlu.normalize(it["name"])],
                                         unit=it.get("unit") or "unidad", track_stock=False)
                        self.db.add(prod)
                        self.db.flush()
                        pid = prod.id
                    measure = it.get("measure")
                    qty = int(measure) if measure and it.get("unit") == "unidad" and float(measure).is_integer() else None
                    mv, _ = ledger.record(self.db, self.b, {
                        "kind": M.SALE, "amount_cents": it["amount_cents"], "method": data["method"], "product_id": pid,
                        "quantity": qty, "measure": None if qty else measure, "measure_unit": None if qty else it.get("unit"),
                        "unit_price_ref_cents": None, "raw_text": data.get("raw_text"), "occurred_at": when},
                        op_id=f"{d.op_id}-{idx}", channel=self.channel, commit=False)
                    mvs.append(mv)
        except ledger.LedgerError as e:
            self.db.rollback()
            return self.resp(f"No pude guardarlo: {e}.")
        d.status = "confirmado"
        d.data = {**data, "movement_ids": [m.id for m in mvs]}
        self.db.commit()
        parts = [short_line(self.db, m, with_method=False) for m in mvs]
        method = "efectivo" if data["method"] == "efectivo" else "Yape (por verificar)"
        return self.resp(f"✓ {', '.join(parts)} · {method}", movement=mvs[-1], quick=["Deshacer"])

    # ------------------------------------------------------------ pérdidas (cualquier rubro)
    def _loss_answer(self, p: nlu.Parsed) -> dict[str, Any]:
        """Respuesta a «¿cuánto fue?»: una cantidad («medio saco») o un monto («30 soles»)."""
        t = p.text
        out: dict[str, Any] = {}
        mm = re.search(r"\b(\d+(?:\.\d+)?) (kilos?|jabas?|sacos?|cajas?|atados?|bolsas?|paquetes?|unidades|docenas?|bandejas?)\b", t)
        if mm:
            out["measure"] = float(mm.group(1))
            out["measure_unit"] = nlu.MEASURE_UNITS.get(mm.group(2), mm.group(2).rstrip("s"))
        lf = nlu.loss_fields(t, self.products())
        amt = lf.get("amount_cents")
        if amt is None and not mm:
            nums = re.findall(r"\d+(?:\.\d{1,2})?", t)
            amt = int(round(float(nums[0]) * 100)) if nums else None
        if amt:
            out["amount_cents"] = amt
        if lf.get("method"):
            out["method"] = lf["method"]
        return out

    def _loss_value(self, data: dict[str, Any], product: M.Product | None) -> int | None:
        if data.get("amount_cents"):
            return int(data["amount_cents"])
        if product is not None and data.get("measure"):
            base = units.to_base(product, data["measure"], data.get("measure_unit") or units.base_unit(product))
            c = (product.spec or {}).get("compra_por") or {}
            if c.get("unidad") == data.get("measure_unit") and c.get("costo_cents"):
                return round(c["costo_cents"] * data["measure"])
            if base is not None and product.cost_cents:
                return round(product.cost_cents * base)
        return None

    def step_loss(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        data = dict(d.data)
        cands = data.get("product_candidates") or []
        if len(cands) > 1 and not data.get("product_id"):
            opts = [{"id": pr.id, "name": pr.name} for pr in (self.db.get(M.Product, c) for c in cands)]
            d.data = data
            return self.ask(d, "product", f"¿Cuál fue: {' o '.join(o['name'].lower() for o in opts)}?", [o["name"] for o in opts], opts)
        product = self._product(data)
        if data.get("loss_kind") == "plata":
            if not data.get("amount_cents"):
                d.data = data
                return self.ask(d, "loss_value", "Qué mala suerte. ¿Cuánta plata fue?")
            if data.get("method") not in M.POCKETS:
                d.data = data
                return self.ask(d, "method", "¿Era plata del efectivo o del Yape?", ["Efectivo", "Yape"])
        else:
            if not data.get("what") and product is None:
                d.data = data
                return self.ask(d, "loss_what", "¿Qué fue lo que se perdió?")
            value = self._loss_value(data, product)
            if value is None:
                d.data = data
                if product is not None and not data.get("measure"):
                    return self.ask(d, "loss_value", f"¿Cuánto {product.name.lower()} fue? Dime la cantidad, por ejemplo «medio saco» o «3 kilos», o cuánto valía.")
                return self.ask(d, "loss_value", "¿Cuánto valía eso, más o menos?")
            data["amount_cents"] = value
        d.data = data
        d.missing = None
        self.db.flush()
        return self.resp("", d)

    def _loss_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        product = self._product(dict(data))
        what = data.get("what") or (product.name.lower() if product else "pérdida")
        return {"kind": M.LOSS, "amount_cents": data["amount_cents"], "product_id": product.id if product else None,
                "measure": data.get("measure"), "measure_unit": data.get("measure_unit"),
                "method": data.get("method") if data.get("loss_kind") == "plata" else None,
                "expense_type": data.get("loss_kind") or ("mercaderia" if product else "otro"),
                "category": data.get("reason") or "pérdida", "description": what, "raw_text": data.get("raw_text"),
                "occurred_at": data.get("occurred_at")}

    def confirm_loss(self, d: M.Draft) -> dict[str, Any]:
        try:
            mv, _ = ledger.record(self.db, self.b, self._loss_payload(d.data), op_id=d.op_id, channel=self.channel, commit=False)
        except ledger.LedgerError as e:
            self.db.rollback()
            return self.resp(f"No pude guardarlo: {e}.")
        d.status = "confirmado"
        d.data = {**d.data, "movement_ids": [mv.id]}
        self.db.commit()
        return self.resp("✓ " + short_line(self.db, mv, with_method=True) + " · lo resté de tu ganancia", movement=mv, quick=["Deshacer"])

    # ------------------------------------------------------------ deshacer
    def undo_last(self) -> dict[str, Any]:
        d = self.db.scalar(select(M.Draft).where(M.Draft.business_id == self.b.id, M.Draft.conversation == self.conv,
                                                 M.Draft.status == "confirmado", M.Draft.action.in_(("movimiento", "ventas", "perdida")))
                           .order_by(M.Draft.updated_at.desc(), M.Draft.id.desc()))
        ids = (d.data or {}).get("movement_ids") if d else None
        mvs = [self.db.get(M.Movement, i) for i in ids] if ids else []
        mvs = [m for m in mvs if m and m.status == "confirmado"]
        if not mvs:
            mv = self.last_movement()
            mvs = [mv] if mv else []
        if not mvs:
            return self.resp("No encontré nada para deshacer.")
        text = ", ".join(short_line(self.db, m, with_method=False) for m in mvs)
        for m in mvs:
            ledger.void(self.db, m, reason="deshecho por la emprendedora", commit=False)
        if d:
            d.status = "deshecho"
        self.db.commit()
        return self.resp(f"Listo, lo borré: {text}.")

    # ------------------------------------------------------------ cierre del día
    def start_close(self, count_only: bool = False) -> dict[str, Any]:
        st = metrics.day_stats(self.db, self.b, self.today)
        by = st["collected_by_method"]
        d = self.new_draft("cierre", {"stage": "contar"})
        d.missing = "counted"
        self.db.commit()
        if count_only:
            return self.resp("Cuenta la plata que tienes ahora en tu monedero. ¿Cuánto es?", d)
        txt = (f"Vamos a cerrar tu día. Hoy registraste {soles(st['sales_cents'])} en ventas: {soles(by['efectivo'])} en efectivo "
               f"y {soles(by['yape'])} por Yape. Ahora cuenta la plata de tu monedero: ¿cuánto tienes?")
        return self.resp(txt, d)

    def collect_message(self, p: nlu.Parsed) -> dict[str, Any]:
        """«¿quién me debe?» / «a quién tengo que cobrar»: la lista es para ella (no se le escribe a la casera)."""
        from . import notify
        txt = notify.cobrar_text(self.db, self.b)
        return self.resp(txt or "Nadie te debe. ¡Bien!")

    def _amount(self, p: nlu.Parsed) -> int | None:
        f = p.fields
        for k in ("counted_cents", "amount_cents", "total_cents", "bare_number", "paid_cents"):
            if f.get(k):
                return int(f[k])
        bal = (f.get("balances") or {}).get("efectivo")
        if bal:
            return int(bal)
        nums = re.findall(r"\d+(?:\.\d+)?", p.text)
        return int(round(float(nums[0]) * 100)) if nums else None

    def close_step(self, d: M.Draft, p: nlu.Parsed, raw: str) -> dict[str, Any]:
        data = dict(d.data)
        stage = data.get("stage")
        t = p.text
        if p.intent == "cancelar" and stage == "contar":
            d.status = "cancelado"
            self.db.commit()
            return self.resp("Ok, cerramos tu día cuando quieras. Di «cerrar mi día».")
        msgs: list[str] = []
        if stage == "contar":
            counted = self._amount(p)
            if counted is None:
                return self.resp("Dime cuánto tienes en efectivo, por ejemplo «tengo 250».", d)
            known = ledger.opening_known(self.db, self.b.id)["efectivo"]
            if not known:
                bal = ledger.pocket_balances(self.db, self.b.id)["efectivo"]
                opening = max(0, counted - bal)
                first = self.db.scalar(select(M.Movement.occurred_at).where(M.Movement.business_id == self.b.id).order_by(M.Movement.occurred_at))
                ledger.record(self.db, self.b, {"kind": M.OPENING, "method": "efectivo", "amount_cents": opening,
                                                "occurred_at": (first - timedelta(minutes=1)) if first else now_lima(), "description": "Saldo inicial (cierre)"},
                              op_id=f"{d.op_id}-saldo", channel=self.channel, commit=False)
                msgs.append(f"Como es tu primer cierre, desde hoy llevo tu caja: tienes {soles(counted)} en efectivo.")
                data["stage"] = "merma"
            else:
                c = reconcile.cash_count(self.db, self.b, "efectivo", counted, commit=False)
                self.db.flush()
                data["count_id"] = c.id
                diff = c.difference or 0
                data["diff"] = diff
                if abs(diff) < 100:
                    msgs.append(f"¡Cuadra! Tienes {soles(counted)}, justo lo que esperaba.")
                    data["stage"] = "merma"
                elif diff > 0:
                    data["stage"] = "sobra"
                    d.data = data
                    d.missing = "sobra"
                    self.db.commit()
                    return self.resp(f"Tienes {soles(counted)} y yo esperaba {soles(c.expected)}: te sobran {soles(diff)}. "
                                     "Seguro son ventas que no alcanzaste a dictar. ¿Las guardo como ventas en efectivo de hoy?",
                                     d, ["Sí, guárdalas", "No"])
                else:
                    data["stage"] = "falta"
                    d.data = data
                    d.missing = "falta"
                    self.db.commit()
                    return self.resp(f"Tienes {soles(counted)} y yo esperaba {soles(c.expected)}: te faltan {soles(-diff)}. ¿Qué pasó?",
                                     d, ["Fue para la casa", "Pagué algo del negocio", "No sé"])
        elif stage == "sobra":
            c = self.db.get(M.Count, data["count_id"])
            if p.intent == "confirmar" or re.search(r"\b(si|guarda|ya|dale|claro)\b", t):
                ledger.record(self.db, self.b, {"kind": M.SALE, "amount_cents": data["diff"], "method": "efectivo",
                                                "description": "ventas no anotadas (cierre del día)", "category": "Ventas no anotadas"},
                              op_id=f"{d.op_id}-sobra", channel=self.channel, commit=False)
                c.review_status = "ajustado"
                msgs.append(f"Guardé {soles(data['diff'])} como ventas en efectivo de hoy.")
            else:
                c.review_status = "pendiente"
                msgs.append("Ok, lo dejo como diferencia por revisar.")
            data["stage"] = "merma"
        elif stage == "falta":
            c = self.db.get(M.Count, data["count_id"])
            amt = -data["diff"]
            if re.search(r"\bcasa\b", t):
                ledger.record(self.db, self.b, {"kind": M.WITHDRAWAL, "amount_cents": amt, "method": "efectivo",
                                                "description": "para la casa (cierre del día)"}, op_id=f"{d.op_id}-falta", channel=self.channel, commit=False)
                c.review_status = "ajustado"
                msgs.append(f"Anoté {soles(amt)} que sacaste para la casa.")
            elif re.search(r"\b(negocio|pague|gasto|compre)\b", t):
                ledger.record(self.db, self.b, {"kind": M.EXPENSE, "amount_cents": amt, "method": "efectivo", "category": "Gastos no anotados",
                                                "expense_type": "variable", "description": "gasto no anotado (cierre del día)"},
                              op_id=f"{d.op_id}-falta", channel=self.channel, commit=False)
                c.review_status = "ajustado"
                msgs.append(f"Anoté {soles(amt)} como gasto del negocio.")
            else:
                c.review_status = "pendiente"
                msgs.append("Ok, lo dejo como diferencia por revisar.")
            data["stage"] = "merma"
        elif stage in ("merma", "merma_monto"):
            if stage == "merma":
                if p.intent == "cancelar" or re.search(r"^(no|nada|ninguno|ninguna|todo bien)\b", t):
                    return self.finish_close(d, data, [])
                if p.intent == "confirmar":
                    d.data = data
                    d.missing = "merma"
                    self.db.commit()
                    return self.resp("¿Qué fue? Dime qué y cuánto, por ejemplo «media jaba de tomate» o «me robaron 20 soles».", d)
                lf = nlu.loss_fields(t, self.products())
                lf["raw_text"] = raw
                if not lf.get("what") and not lf.get("product_candidates"):
                    lf["what"] = raw.strip()
                data["loss"] = lf
            else:
                lf = dict(data.get("loss") or {})
                lf.update(self._loss_answer(p))
                data["loss"] = lf
            lf = data["loss"]
            cands = lf.get("product_candidates") or []
            product = self.db.get(M.Product, cands[0]) if cands else None
            if product is not None:
                lf["product_id"] = product.id
            if lf.get("loss_kind") == "plata" and lf.get("method") not in M.POCKETS:
                lf["method"] = "efectivo"
            value = self._loss_value(lf, product)
            if value is None:
                data["stage"] = "merma_monto"
                d.data = data
                d.missing = "merma_monto"
                self.db.commit()
                return self.resp("¿Cuánto valía eso, más o menos?" if not (product and not lf.get("measure"))
                                 else f"¿Cuánto {product.name.lower()} fue? Por ejemplo «medio saco», «3 kilos», o cuánto valía.", d)
            lf["amount_cents"] = value
            mv, _ = ledger.record(self.db, self.b, self._loss_payload(lf), op_id=f"{d.op_id}-merma", channel=self.channel, commit=False)
            self.db.flush()
            return self.finish_close(d, data, [f"Anoté la pérdida: {short_line(self.db, mv, with_method=False).replace('pérdida ', '')}."])
        d.data = data
        d.missing = data["stage"]
        self.db.commit()
        return self.resp(" ".join(msgs + ["¿Tuviste alguna pérdida hoy? Algo que se malogró, se venció, se rompió o te robaron. Dime qué y cuánto, o «no»."]), d, ["No, nada"])

    def finish_close(self, d: M.Draft, data: dict[str, Any], msgs: list[str]) -> dict[str, Any]:
        d.status = "confirmado"
        d.data = {**data, "stage": "listo"}
        self.db.commit()
        st = metrics.day_stats(self.db, self.b, self.today)
        cash = metrics.cash_position(self.db, self.b)
        s0, e0 = datetime(self.today.year, self.today.month, self.today.day), datetime(self.today.year, self.today.month, self.today.day) + timedelta(days=1)
        yapes = [m for m in metrics.movements(self.db, self.b.id, s0, e0) if ledger.yape_direction(m) and not ledger.yape_verified(self.db, m)]
        from . import notify
        day_txt = notify.daily_text(self.db, self.b, self.today)
        txt = msgs + ["Listo, tu día está cerrado.", day_txt]
        if cash["complete"]:
            txt.append(f"Tienes {soles(cash['efectivo_cents'])} en efectivo y {soles(cash['yape_cents'])} en Yape.")
        if yapes:
            one = len(yapes) == 1
            txt.append(f"Revisa en tu Yape {len(yapes)} {'movimiento' if one else 'movimientos'} de hoy "
                       f"({soles(sum(m.paid_cents for m in yapes))}) que aún no {'está verificado' if one else 'están verificados'}.")
        notify.mark_night_sent(self.db, self.b, day_txt)
        return self.resp(" ".join(txt), quick=["Resumen de hoy"])

    # ------------------------------------------------------------ catálogo
    def step_catalog(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        data = dict(d.data)
        cands = data.get("product_candidates") or []
        if len(cands) > 1 and not data.get("product_id"):
            opts = [{"id": p.id, "name": p.name} for p in (self.db.get(M.Product, c) for c in cands)]
            d.data = data
            return self.ask(d, "product", f"¿De cuál hablas: {' o '.join(o['name'].lower() for o in opts)}?",
                            [o["name"] for o in opts], opts)
        product = self._product(data)
        if not product and not data.get("new_product"):
            d.data = data
            return self.ask(d, "product", "¿De qué producto?", [p.name for p in self.products()[:4]],
                            [{"id": p.id, "name": p.name} for p in self.products()])
        if data.get("ambiguous_cents") is not None:
            d.data = data
            return self.ask(d, "catalog_role",
                            f"¿{soles(data['ambiguous_cents'])} es el precio al que lo vendes o lo que te cuesta comprarlo?",
                            ["Lo vendo a ese precio", "Me cuesta eso"])
        name = product.name if product else cap(data["new_product"])
        parts = []
        if data.get("price_cents") is not None:
            parts.append(f"precio de venta {soles(data['price_cents'])}")
        if data.get("cost_cents") is not None:
            parts.append(f"costo de compra {soles(data['cost_cents'])}")
        if data.get("stock") is not None:
            parts.append(f"{data['stock']} en stock")
        if not parts:
            d.data = data
            return self.ask(d, "catalog_value", f"¿Qué dato de {name.lower()} quieres guardar: precio, costo o stock?")
        d.data = data
        d.missing = None
        d.summary_read = True
        self.db.commit()
        return self.resp(f"{name}: {', '.join(parts)}. ¿Lo guardo?", d, ["Sí, guardar", "Cancelar"])

    def confirm_catalog(self, d: M.Draft) -> dict[str, Any]:
        data = d.data
        product = self._product(dict(data))
        if not product:
            nm = cap(data["new_product"])
            product = M.Product(business_id=self.b.id, name=nm, aliases=[nlu.normalize(nm)],
                                track_stock=self.b.kind == "reventa", is_service=self.b.kind == "servicios")
            self.db.add(product)
            self.db.flush()
        msg = []
        if data.get("price_cents") is not None:
            product.price_cents = data["price_cents"]
        if data.get("cost_cents") is not None:
            product.cost_cents = data["cost_cents"]
        if data.get("stock") is not None:
            if product.initial_stock is None:
                product.initial_stock = data["stock"] - ledger.stock_delta(self.db, product.id)
                msg.append(f"Desde ahora llevo la cuenta: {data['stock']} en stock.")
            else:
                c = reconcile.stock_count(self.db, self.b, product, data["stock"], commit=False)
                self.db.flush()
                msg.append(reconcile.count_text(self.db, c))
        d.status = "confirmado"
        self.db.commit()
        return self.resp(f"Listo, actualicé {product.name.lower()}. " + " ".join(msg))

    # ------------------------------------------------------------ saldo inicial
    def step_balance(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        bal = d.data["balances"]
        txt = " y ".join(f"{soles(v)} en {'Yape' if k == 'yape' else 'efectivo'}" for k, v in bal.items())
        d.summary_read = True
        d.missing = None
        self.db.commit()
        return self.resp(f"Tu saldo inicial: {txt}. ¿Lo guardo?", d, ["Sí, guardar", "Cancelar"])

    def confirm_balance(self, d: M.Draft) -> dict[str, Any]:
        known = ledger.opening_known(self.db, self.b.id)
        for pocket, cents in d.data["balances"].items():
            if known.get(pocket):
                old = self.db.scalar(select(M.Movement).where(M.Movement.business_id == self.b.id, M.Movement.kind == M.OPENING,
                                                              M.Movement.method == pocket, M.Movement.status == "confirmado"))
                ledger.revise(self.db, self.b, old, {"amount_cents": cents}, reason="saldo inicial corregido", commit=False)
            else:
                first = self.db.scalar(select(M.Movement.occurred_at).where(M.Movement.business_id == self.b.id)
                                       .order_by(M.Movement.occurred_at))
                when = (first - timedelta(minutes=1)) if first else now_lima()
                ledger.record(self.db, self.b, {"kind": M.OPENING, "method": pocket, "amount_cents": cents,
                                                "occurred_at": when, "description": "Saldo inicial"},
                              op_id=f"{d.op_id}-{pocket}", channel=self.channel, commit=False)
        d.status = "confirmado"
        self.db.commit()
        return self.resp("Listo. " + summaries.cash_line(self.db, self.b))

    # ------------------------------------------------------------ conteo
    def step_count(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        data = dict(d.data)
        cands = data.get("product_candidates") or []
        if len(cands) > 1 and not data.get("product_id"):
            opts = [{"id": p.id, "name": p.name} for p in (self.db.get(M.Product, c) for c in cands)]
            d.data = data
            return self.ask(d, "product", f"¿Cuál contaste: {' o '.join(o['name'].lower() for o in opts)}?",
                            [o["name"] for o in opts], opts)
        product = self._product(data)
        c = reconcile.stock_count(self.db, self.b, product, data["counted"], commit=False)
        d.status = "confirmado"
        self.db.commit()
        quick = ["Ajustar stock", "Dejar pendiente"] if c.result == "diferencia" else []
        return self.resp(reconcile.count_text(self.db, c), quick=quick, count=metrics._count_dict(c))

    # ------------------------------------------------------------ metas
    def step_goal(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        data = dict(d.data)
        if not data.get("target_cents"):
            d.data = data
            return self.ask(d, "amount", "¿Cuánto necesitas juntar?")
        if not data.get("name") and data.get("ask_purpose") and not data.get("purpose_asked"):
            data["purpose_asked"] = True
            d.data = data
            return self.ask(d, "name", f"Meta de {soles(data['target_cents'])} 👍 ¿Para qué es? (por ejemplo: una congeladora, mercadería, abrir un local)",
                            ["Reponer mercadería", "Comprar un equipo", "Abrir un local"])
        if data.get("ask_purpose") and data.get("name"):
            data["name"] = re.sub(r"^(?:para )?(?:(?:una|un|la|el|mi|mis)\s+)?", "", data["name"], flags=re.I).strip().capitalize() or data["name"]
        if not data.get("name"):
            data["name"] = "Reponer mercadería" if data.get("kind") == "reposicion" else "Mi meta"
        d.data = data
        txt = f"Meta «{data['name']}»: {soles(data['target_cents'])}"
        if data.get("deadline"):
            dl = date.fromisoformat(data["deadline"])
            days = max(1, (dl - self.today).days)
            txt += f" para el {dl.day}/{dl.month}. Tendrías que separar {soles(round(data['target_cents'] / days * 7))} por semana"
            st30 = metrics.period_stats(self.db, self.b, self.today - timedelta(days=30), self.today + timedelta(days=1))
            if st30["profit_cents"] > 0:
                share = round(100 * (data["target_cents"] / days * 30) / st30["profit_cents"])
                txt += f", el {share}% de lo que ganas al mes"
        elif data.get("kind") == "reposicion":
            cash = metrics.cash_position(self.db, self.b)
            if cash["available_for_restock_cents"] is not None:
                txt += f". Hoy tienes {soles(max(0, cash['available_for_restock_cents']))} disponibles"
        d.missing = None
        d.summary_read = True
        self.db.commit()
        return self.resp(txt + ". ¿La guardo?", d, ["Sí, guardar", "Cancelar"])

    def confirm_goal(self, d: M.Draft) -> dict[str, Any]:
        data = d.data
        g = M.Goal(business_id=self.b.id, kind=data.get("kind", "ahorro"), name=data["name"],
                   target_cents=data["target_cents"],
                   deadline=date.fromisoformat(data["deadline"]) if data.get("deadline") else None)
        self.db.add(g)
        d.status = "confirmado"
        self.db.commit()
        st = metrics.goal_status(self.db, self.b, g)
        out = "Meta guardada. " + st["message"]
        try:   # además: cómo llegar y qué producto financiero ayuda (si algo falla, queda el mensaje de siempre)
            from . import finance_coach
            out += "\n\n" + finance_coach.goal_plan(self.db, self.b, name=g.name, target_cents=g.target_cents, deadline=g.deadline,
                                                    today=self.today, kind=g.kind)
        except Exception:   # noqa: BLE001
            pass
        return self.resp(out)

    # ------------------------------------------------------------ correcciones
    def last_movement(self) -> M.Movement | None:
        return self.db.scalar(select(M.Movement).where(M.Movement.business_id == self.b.id, M.Movement.status == "confirmado",
                                                       M.Movement.kind.not_in((M.OPENING, M.LOSS))).order_by(M.Movement.created_at.desc(), M.Movement.id.desc()))

    def start_correction(self, p: nlu.Parsed) -> dict[str, Any]:
        mv = None
        cands = p.fields.get("product_candidates") or []
        if len(cands) == 1:   # «corrige, el tomate fue 4»: la última venta/compra de ESE producto, no la última en general
            mv = self.db.scalar(select(M.Movement).where(
                M.Movement.business_id == self.b.id, M.Movement.status == "confirmado", M.Movement.product_id == cands[0],
                M.Movement.kind.in_((M.SALE, M.PURCHASE, M.LOSS))).order_by(M.Movement.created_at.desc(), M.Movement.id.desc()))
            if mv:
                p.fields = {k: v for k, v in p.fields.items() if k != "product_candidates"}
        mv = mv or self.last_movement()
        if not mv:
            return self.resp("No encontré un movimiento para corregir.")
        d = self.new_draft("correccion", {"movement_id": mv.id, "changes": self._changes(mv, p.fields)})
        return self.step(d)

    def _changes(self, mv: M.Movement, f: dict[str, Any]) -> dict[str, Any]:
        ch: dict[str, Any] = {}
        if f.get("method") in M.POCKETS:
            ch["method"] = f["method"]
        if f.get("quantity"):
            ch["quantity"] = f["quantity"]
        amount = f.get("total_cents") or f.get("amount_cents") or f.get("bare_number")
        if amount and amount != (f.get("quantity") or 0) * 100:
            ch["amount_cents"] = amount
        if f.get("product_candidates") and len(f["product_candidates"]) == 1 and f["product_candidates"][0] != mv.product_id:
            ch["product_id"] = f["product_candidates"][0]
        return ch

    def step_correction(self, d: M.Draft, auto_confirm: bool = False) -> dict[str, Any]:
        mv = self.db.get(M.Movement, d.data["movement_id"])
        ch = d.data.get("changes") or {}
        if not ch:
            return self.ask(d, "changes", f"¿Qué corrijo de tu {MOVEMENT_LABEL[mv.kind]} ({describe_movement(self.db, mv)})? "
                                          "Dime por ejemplo «eran dos», «fue por Yape» o «fue 45».")
        preview = dict(ledger.snapshot(mv))
        preview.update(ch)
        txt = f"Corrijo tu {MOVEMENT_LABEL[mv.kind]}: antes {describe_movement(self.db, mv)}; ahora {describe_snapshot(self.db, preview)}. ¿Lo guardo?"
        d.missing = None
        d.summary_read = True
        self.db.commit()
        return self.resp(txt, d, ["Sí, guardar", "Cancelar"])

    def confirm_correction(self, d: M.Draft) -> dict[str, Any]:
        mv = self.db.get(M.Movement, d.data["movement_id"])
        try:
            ledger.revise(self.db, self.b, mv, d.data["changes"], reason="corrección por chat", commit=False)
        except ledger.LedgerError as e:
            self.db.rollback()
            return self.resp(f"No pude corregirlo: {e}.")
        d.status = "confirmado"
        self.db.commit()
        return self.resp(f"Corregido. Quedó: {describe_movement(self.db, mv)}. Guardé el cambio en el historial.", movement=mv)

    def confirm_void(self, d: M.Draft) -> dict[str, Any]:
        mv = self.db.get(M.Movement, d.data["movement_id"])
        ledger.void(self.db, mv, reason="anulado por chat", commit=False)
        d.status = "confirmado"
        self.db.commit()
        return self.resp("Anulado. Revertí sus efectos en caja y stock y quedó en el historial.", movement=mv)

    # ------------------------------------------------------------ consentimiento mensual BCP
    def pending_consent_month(self) -> date | None:
        prev = add_months(month_start(self.today), -1)
        key = prev.strftime("%Y-%m")
        if self.db.scalar(select(M.ReportConsent).where(M.ReportConsent.business_id == self.b.id, M.ReportConsent.month == key)):
            return None
        has = self.db.scalar(select(M.Movement.id).where(M.Movement.business_id == self.b.id,
                                                         M.Movement.occurred_at >= datetime(prev.year, prev.month, 1),
                                                         M.Movement.occurred_at < datetime(self.today.year, self.today.month, 1)))
        return prev if has else None

    def with_consent_prompt(self, r: dict[str, Any]) -> dict[str, Any]:
        if r.get("draft") or r.get("share"):
            return r
        prev = self.pending_consent_month()
        if not prev:
            return r
        already = self.db.scalar(select(M.Draft).where(M.Draft.business_id == self.b.id, M.Draft.action == "consentimiento",
                                                       M.Draft.conversation == self.conv))
        if already and already.data.get("month") == prev.strftime("%Y-%m"):
            return r
        d = self.new_draft("consentimiento", {"month": prev.strftime("%Y-%m")})
        d.summary_read = True
        self.db.commit()
        r["reply"] += f" Por cierto: ya está tu informe de {month_label(prev)}. ¿Quieres que se le envíe al BCP?"
        r["draft"] = self.draft_view(d)
        r["quick_replies"] = ["Sí, envíalo", "No por ahora"]
        return r

    def consent(self, yes: bool, d: M.Draft | None = None) -> dict[str, Any]:
        prev = add_months(month_start(self.today), -1)
        key = d.data["month"] if d else prev.strftime("%Y-%m")
        existing = self.db.scalar(select(M.ReportConsent).where(M.ReportConsent.business_id == self.b.id, M.ReportConsent.month == key))
        if not existing:
            self.db.add(M.ReportConsent(business_id=self.b.id, month=key, decision="si" if yes else "no"))
        if d:
            d.status = "confirmado"
        self.db.commit()
        self.db.commit()
        if yes:
            return self.resp("Listo, autorizaste el envío de tu informe al BCP. En esta demo el envío es simulado: "
                             "puedes revisarlo y descargarlo en «Reporte».")
        return self.resp("Ok, no lo envío. Lo tienes en «Reporte» cuando lo necesites.")

    # ------------------------------------------------------------ onboarding por chat
    def onboarding(self, text: str) -> dict[str, Any]:
        t = nlu.normalize(text)
        if self.b.setup_state == "pendiente":
            if len(t.split()) < 3:
                return self.resp("¡Hola! Soy tu asistente de números. Cuéntame qué vendes y cómo te pagan.",
                                 quick=["Vendo polos y cobro por Yape y efectivo", "Vendo menú", "Hago cortes de cabello"])
            p = setup.propose(self.db, self.b, text)
            return self.resp(setup.proposal_text(p), quick=["Sí, guardar", "Usar plantilla de reventa", "Usar plantilla de comida", "Usar plantilla de servicios"],
                             setup=p)
        # propuesta en espera
        if nlu.CONFIRM_RE.match(t):
            setup.apply(self.db, self.b)
            return self.resp("¡Listo, tu negocio está configurado! Ya puedes dictarme tus ventas. "
                             "Si quieres, dime tu saldo inicial: «empiezo con 200 en efectivo y 100 en Yape».")
        for kind in ("reventa", "comida", "servicios"):
            if kind in t:
                setup.from_template(self.db, self.b, kind)
                return self.resp(f"Listo, usé la plantilla de {kind}. Ya puedes dictarme tus ventas.")
        if nlu.CANCEL_RE.match(t) and len(t.split()) <= 3:
            self.b.setup_state = "pendiente"
            self.db.commit()
            return self.resp("Ok, cuéntame de nuevo qué vendes y cómo te pagan.")
        p = setup.propose(self.db, self.b, text)
        return self.resp(setup.proposal_text(p), quick=["Sí, guardar", "Usar plantilla de reventa"], setup=p)


# ------------------------------------------------------------------ vistas

AUTO_SAVE = {"movimiento", "ventas", "correccion", "saldo", "meta", "catalogo", "perdida"}


def measure_txt(measure: float | None, unit: str | None) -> str:
    if not measure:
        return ""
    frac = {0.25: "¼", 0.5: "½", 0.75: "¾"}
    n = frac.get(measure) or (str(int(measure)) if float(measure).is_integer() else f"{measure:g}")
    u = {"kg": "kg", "unidad": "", None: ""}.get(unit, unit or "")
    if u and u != "kg" and measure > 1:
        u = units.unit_name(unit, measure)   # «2 atados», «3 jabas»
    return f"{n} {u}".strip()


def short_line(db: Session, mv: M.Movement, with_method: bool = True) -> str:
    """Texto corto de confirmación: «S/ 3 tomate», «S/ 50 tres polos básicos · efectivo · desc. S/ 10»."""
    amt = soles(mv.amount_cents)
    k = mv.kind
    name = mv.product.name.lower() if mv.product else None
    if k == M.SALE:
        if name and mv.quantity and not mv.measure and units.base_unit(mv.product) not in ("unidad", "plato", "docena"):
            what = f"{name} ({units.fmt_qty(mv.quantity)} {units.unit_name(units.base_unit(mv.product), mv.quantity)})"
        elif name and mv.quantity and not (mv.product and not mv.product.track_stock and mv.measure):
            what = qty_name(mv.quantity, name) if mv.product.track_stock else f"{mv.quantity} {name}"
        elif name and mv.measure:
            what = f"{name} ({measure_txt(mv.measure, mv.measure_unit)})"
        else:
            what = name or (mv.description or "ventas varias")
        s = f"{amt} {what}"
    elif k == M.PURCHASE:
        if name and mv.measure:
            s = f"compra {amt} {name} ({units.fmt_qty(mv.measure)} {units.unit_name(mv.measure_unit, mv.measure)})"
        else:
            s = f"compra {amt}" + (f" {qty_name(mv.quantity, name)}" if name and mv.quantity else (f" {name}" if name else " de mercadería"))
    elif k == M.EXPENSE:
        s = f"gasto {amt} {(mv.category or 'del negocio').lower()}"
    elif k == M.WITHDRAWAL:
        s = f"{amt} para la casa"
    elif k == M.CONTRIBUTION:
        s = f"aporte {amt}"
    elif k == M.TRANSFER:
        s = f"pasaste {amt} de {mv.method} a {mv.to_method}"
    elif k == M.COLLECTION:
        s = f"{mv.counterparty or 'tu cliente'} te pagó {amt}"
    elif k == M.DEBT_PAYMENT:
        s = f"cuota {amt}"
    elif k == M.GOAL_SAVING:
        g = db.get(M.Goal, mv.goal_id) if mv.goal_id else None
        s = f"{amt} para «{g.name if g else 'tu meta'}»"
    elif k == M.LOSS:
        what = mv.description or (name or "pérdida")
        if name and mv.measure:
            what = f"{name} ({units.fmt_qty(mv.measure)} {units.unit_name(mv.measure_unit, mv.measure)})"
        s = f"pérdida {amt} · {what}" + (f" ({mv.category})" if mv.category and mv.category not in ("pérdida", "Merma") else "")
    else:
        s = amt
    extra = []
    if with_method and mv.method in M.POCKETS and k != M.TRANSFER:
        if mv.method == "efectivo":
            extra.append("efectivo")
        elif ledger.yape_direction(mv):
            extra.append("Yape ✓ verificado" if ledger.yape_verified(db, mv) else "Yape (por verificar)")
        else:
            extra.append("Yape")
    if k == M.SALE and mv.discount_cents:
        extra.append(f"desc. {soles(mv.discount_cents)}")
    if k == M.SALE:
        pend = ledger.sale_pending(db, mv)
        if pend:
            extra.append(f"te debe {soles(pend)}" + (f" ({mv.counterparty})" if mv.counterparty else ""))
        if mv.product and mv.product.track_stock:
            exp = ledger.expected_stock(db, mv.product)
            if exp is not None:
                u = units.base_unit(mv.product)
                tail = "" if u == "unidad" else (" kg" if u == "kg" else f" {units.unit_name(u, exp)}")
                extra.append(f"quedan {units.fmt_qty(exp)}{tail}" if exp >= 0 else f"ojo: stock negativo ({units.fmt_qty(exp)})")
    if k == M.COLLECTION and mv.parent_id:
        pend = ledger.sale_pending(db, db.get(M.Movement, mv.parent_id))
        extra.append(f"aún te debe {soles(pend)}" if pend else "ya no te debe nada")
    if k == M.GOAL_SAVING and mv.goal_id:
        extra.append(f"vas al {metrics.goal_status(db, db.get(M.Business, mv.business_id), db.get(M.Goal, mv.goal_id))['progress_pct']}%")
    return s + "".join(f" · {e}" for e in extra)

def describe_snapshot(db: Session, s: dict[str, Any]) -> str:
    p = db.get(M.Product, s["product_id"]) if s.get("product_id") else None
    k = s["kind"]
    amt = soles(s["amount_cents"])
    if k in (M.SALE, M.PURCHASE) and p:
        return f"{qty_name(s.get('quantity'), p.name)}, {amt}{_m(s.get('method'))}"
    return f"{amt}{_m(s.get('method'))}"


def describe_movement(db: Session, mv: M.Movement) -> str:
    return describe_snapshot(db, ledger.snapshot(mv))


def movement_view(db: Session, mv: M.Movement) -> dict[str, Any]:
    from .report import CHECK_LABEL, ORIGIN_LABEL
    return {
        "id": mv.id, "op_id": mv.op_id, "kind": mv.kind, "label": MOVEMENT_LABEL.get(mv.kind, mv.kind),
        "occurred_at": mv.occurred_at.isoformat(), "amount_cents": mv.amount_cents, "paid_cents": mv.paid_cents,
        "pending_cents": ledger.sale_pending(db, mv) if mv.kind == M.SALE else 0,
        "method": mv.method, "to_method": mv.to_method, "product_id": mv.product_id,
        "product": mv.product.name if mv.product else None, "quantity": mv.quantity,
        "unit_price_ref_cents": mv.unit_price_ref_cents, "reference_total_cents": mv.reference_total_cents,
        "discount_cents": mv.discount_cents, "unit_cost_cents": mv.unit_cost_cents,
        "margin_cents": (mv.amount_cents - mv.unit_cost_cents * mv.quantity) if mv.kind == M.SALE and mv.unit_cost_cents is not None and mv.quantity else None,
        "units_pending": mv.units_pending, "category": mv.category, "expense_type": mv.expense_type,
        "counterparty": mv.counterparty, "description": mv.description, "raw_text": mv.raw_text,
        "channel": mv.channel, "origin": mv.origin, "origin_label": ORIGIN_LABEL.get(mv.origin, mv.origin),
        "check": reconcile.sale_check(db, mv) if mv.kind in (M.SALE, M.PURCHASE) else None,
        "check_label": CHECK_LABEL[reconcile.sale_check(db, mv)] if mv.kind in (M.SALE, M.PURCHASE) else None,
        "status": mv.status, "revision": mv.revision, "demo": mv.demo, "parent_id": mv.parent_id,
        "yape_direction": ledger.yape_direction(mv), "yape_verified": ledger.yape_verified(db, mv) is not None,
        "attachments": [{"id": a.id, "kind": a.kind, "filename": a.filename, "demo": a.demo} for a in mv.attachments],
    }


def process_message(db: Session, business: M.Business, text: str, *, message_id: str, channel: str = "web",
                    conversation: str | None = None, op_id: str | None = None, occurred_at: datetime | None = None,
                    via_voice: bool = False, transcript: str | None = None, agent_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Punto de entrada idempotente: el mismo message_id nunca se procesa dos veces."""
    prev = db.scalar(select(M.ProcessedMessage).where(M.ProcessedMessage.message_id == message_id))
    if prev:
        r = dict(prev.response)
        r["duplicate"] = True
        return r
    eng = Engine(db, business, conversation or f"{channel}:{business.id}", channel)
    db.add(M.ChatLog(business_id=business.id, role="user", text=text, channel=channel, via_voice=via_voice))
    r = eng.handle(text, op_id=op_id, occurred_at=occurred_at, agent_data=agent_data)
    if agent_data:
        r["agent"] = True
    if transcript is not None:
        r["transcript"] = transcript
    r["message_id"] = message_id
    db.add(M.ChatLog(business_id=business.id, role="bot", text=r["reply"], channel=channel))
    db.add(M.ProcessedMessage(message_id=message_id, business_id=business.id, channel=channel, text=text,
                              response=_jsonable(r)))
    db.commit()
    return r


def _jsonable(o):
    import json
    return json.loads(json.dumps(o, default=str))
