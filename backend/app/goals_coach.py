"""Coach de metas: apenas termina la primera conversación, Kallpa propone metas con los números del negocio,
pregunta cuál quiere empezar y responde «¿por qué?» en corto. También se activa con «metas» si no tiene ninguna.

Las propuestas salen de lo que la persona contó: lo que cuesta reponer su mercadería, sus gastos fijos
(con la fecha de su pago más grande) y un colchón para emergencias.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from . import metrics
from . import models as M
from . import nlu
from .money import soles

ORD = {"1": 0, "primera": 0, "primero": 0, "la 1": 0, "opcion 1": 0,
       "2": 1, "dos": 1, "segunda": 1, "segundo": 1,
       "3": 2, "tres": 2, "tercera": 2, "tercero": 2}
KEYWORDS = [(r"\b(repon|mercaderia|compra|surtir|llenar)", "reponer"),
            (r"\b(fijo|fijos|alquiler|luz|pagos|gastos)", "fijos"),
            (r"\b(colchon|emergencia|imprevist|ahorro|guardadito)", "colchon")]


def _round10(cents: int) -> int:
    return max(1000, int(round(cents / 1000.0)) * 1000)


def _fmt_day(d: date) -> str:
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
    return f"{d.day} de {meses[d.month - 1]}"


def proposals(db, b: M.Business, today: date) -> list[dict[str, Any]]:
    from . import units
    props: list[dict[str, Any]] = []
    prods = db.scalars(select(M.Product).where(M.Product.business_id == b.id, M.Product.active.is_(True))).all()

    # 1) Reponer: lo que cuesta volver a llenar el puesto como cuando empezó
    restock, names = 0, []
    for p in prods:
        spec = p.spec or {}
        c = spec.get("compra_por") or {}
        if c.get("costo_cents") and c.get("unidad") not in (None, units.base_unit(p)):
            restock += c["costo_cents"]
            names.append(p.name.lower())
        elif p.cost_cents and p.initial_stock:
            restock += round(p.cost_cents * p.initial_stock)
            names.append(p.name.lower())
    if restock:
        target = _round10(restock)
        dl = today + timedelta(days=7)
        props.append({
            "key": "reponer", "kind": "reposicion", "name": "Reponer mi mercadería", "target_cents": target,
            "deadline": dl.isoformat(), "per_day_cents": round(target / 7),
            "title": f"Reponer tu mercadería: juntar {soles(target)} en 7 días",
            "why": (f"Es lo que te cuesta volver a llenar tu puesto ({', '.join(names[:3])}…). Si la juntas, nunca te quedas "
                    "sin mercadería ni tienes que pedir prestado o comprar fiado más caro."),
        })

    # 2) Gastos fijos del mes, para la fecha del pago más grande
    fixed = metrics.declared_costs(db, b)["fijo"]
    if fixed:
        big = db.scalar(select(M.FixedExpense).where(M.FixedExpense.business_id == b.id, M.FixedExpense.cost_type == "fijo",
                                                     M.FixedExpense.frequency == "mensual")
                        .order_by(M.FixedExpense.amount_cents.desc()))
        day = big.due_day if big else 1
        dl = date(today.year, today.month, min(day, 28))
        if dl <= today + timedelta(days=6):
            nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
            dl = date(nxt.year, nxt.month, min(day, 28))
        days = max(1, (dl - today).days)
        target = _round10(fixed)
        props.append({
            "key": "fijos", "kind": "ahorro", "name": "Mis gastos fijos del mes", "target_cents": target,
            "deadline": dl.isoformat(), "per_day_cents": round(target / days),
            "title": f"Tus gastos fijos del mes: {soles(target)} para el {_fmt_day(dl)}",
            "why": (f"Son tus pagos de cada mes ({big.name.lower() if big else 'alquiler'}, luz…). Separando "
                    f"{soles(round(target / days))} al día llegas tranquila, sin usar la plata de tu mercadería."),
        })

    # 3) Colchón: dos semanas de gastos fijos (o S/ 300 si aún no los dijo)
    base = round(fixed / 2) if fixed else 30000
    target = _round10(max(base, 20000))
    dl = today + timedelta(days=60)
    props.append({
        "key": "colchon", "kind": "ahorro", "name": "Colchón para emergencias", "target_cents": target,
        "deadline": dl.isoformat(), "per_day_cents": round(target / 60),
        "title": f"Un colchón para emergencias: {soles(target)} en 2 meses",
        "why": ("Si un día no vendes, se malogra mercadería o te enfermas, tienes de dónde sacar sin endeudarte. "
                f"Son solo {soles(round(target / 60))} al día."),
    })
    return props[:3]


ASK_AMOUNT_HEADER = ("🎯 ¡Buena idea ponerte una meta! Cuéntame de cuánto la quieres y para qué (por ejemplo: «1500 para una "
                     "refrigeradora en 3 meses») y te armo el plan para llegar. Si aún no lo tienes claro, te propongo con tus números:")
ASK_AMOUNT_FOOTER = "💬 O dime tu propia meta: de cuánto y para qué."


def intro(props: list[dict[str, Any]], ask_amount: bool = False) -> str:
    nums = ["1️⃣", "2️⃣", "3️⃣"]
    lines = [ASK_AMOUNT_HEADER if ask_amount else
             "🎯 Ahora lo más importante: ponerte una meta. Con una meta separas un poquito cada día y llegas. Te propongo:"]
    for i, p in enumerate(props):
        lines.append(f"{nums[i]} {p['title']} ({soles(p['per_day_cents'])} al día).")
    n = [str(i + 1) for i in range(len(props))]
    opts = (", ".join(n[:-1]) + " o " + n[-1]) if len(n) > 1 else n[0]
    lines.append(f"¿Con cuál te gustaría comenzar? Responde {opts}. Si quieres saber por qué, pregúntame «¿por qué la 1?».")
    if ask_amount:
        lines.append(ASK_AMOUNT_FOOTER)
    return "\n".join(lines)


def quick(props: list[dict[str, Any]]) -> list[str]:
    return [str(i + 1) for i in range(len(props))] + ["¿Por qué?"]


def _which(t: str, props: list[dict[str, Any]], last: int | None) -> list[int]:
    if re.search(r"\b(todas|las tres|las 3|todos)\b", t):
        return list(range(len(props)))
    found: list[int] = []
    for w, i in ORD.items():
        if re.search(rf"(?<![\d.]){re.escape(w)}\b", t) and i < len(props) and i not in found:
            found.append(i)
    for pat, key in KEYWORDS:
        if re.search(pat, t):
            for i, p in enumerate(props):
                if p["key"] == key and i not in found:
                    found.append(i)
    if not found and last is not None and re.search(r"\b(esa|esta|ese|eso|la misma|dale|ya|ok|si|me gusta)\b", t):
        found = [last]
    return found


REGISTRO = ("movimiento", "perdida", "ahorro_meta", "catalogo", "saldo_inicial", "conteo_stock", "conteo_caja",
            "cierre", "corregir", "anular", "deshacer", "cobrar", "gasto_fijo", "contar_plata")


def reply(eng, d: M.Draft, raw: str, intent: str = "") -> dict[str, Any] | None:
    """Responde dentro de la propuesta. None = la persona hizo otra cosa (una venta, una pregunta): se sigue normal."""
    data = dict(d.data or {})
    props = data.get("props") or []
    t = nlu.normalize(raw)
    if not props or intent in REGISTRO:
        return None
    if len(t.split()) > 12 and not re.search(r"\b(meta|por que|porque)\b", t):
        return None
    # Dijo su propia meta (monto y/o para qué): se arma esa meta en vez de elegir una propuesta (ej. «en 3 meses» no es la opción 3)
    try:
        from . import finance_coach
        own = finance_coach.custom_goal_fields(raw, eng.today)
    except Exception:   # noqa: BLE001 (si algo falla, sigue el flujo de siempre)
        own = None
    if own:
        d.status = "cancelado"
        return eng.step(eng.new_draft("meta", own))
    if re.match(r"^(ninguna|ninguno|despues|luego|mas tarde|ahora no|no gracias|no quiero|no por ahora|no)\b", t):
        d.status = "cancelado"
        eng.db.commit()
        return eng.resp("Ok 👍. Cuando quieras, escribe «metas» y te las vuelvo a proponer.")
    # «¿por qué?»
    if re.search(r"\b(por que|porque|para que|de donde|como sale|explica|por q|xq)\b", t):
        idx = _which(t, props, None)
        if idx:
            i = idx[0]
            data["last"] = i
            d.data = data
            eng.db.commit()
            return eng.resp(f"{props[i]['why']} ¿La empezamos?", d, ["Sí, esa", "Ver las otras"])
        d.data = data
        eng.db.commit()
        return eng.resp("Porque con una meta clara separas un poquito cada día en vez de esperar a que «sobre». "
                        "Te propuse metas con tus propios números: lo que cuesta reponer, tus pagos del mes y un colchón. "
                        "¿Cuál quieres empezar?", d, quick(props))
    if re.search(r"\b(ver las otras|las otras|cuales eran|repite|de nuevo|opciones)\b", t):
        return eng.resp(intro(props), d, quick(props))
    idx = _which(t, props, data.get("last"))
    if not idx:
        return None
    # monto propio: «la 1 pero con 200»
    amt = None
    m = re.search(r"\b(?:con|de|a|por|pero)\s+(?:s/ ?)?(\d+(?:\.\d+)?)\s*(?:soles)?\b", t)
    if m and len(idx) == 1 and float(m.group(1)) > 3:
        amt = int(round(float(m.group(1)) * 100))
    msgs = []
    for i in idx:
        p = dict(props[i])
        if amt:
            p["target_cents"] = amt
        g = M.Goal(business_id=eng.b.id, kind=p["kind"], name=p["name"], target_cents=p["target_cents"],
                   deadline=date.fromisoformat(p["deadline"]) if p.get("deadline") else None)
        eng.db.add(g)
        dl = date.fromisoformat(p["deadline"])
        per_day = round(p["target_cents"] / max(1, (dl - eng.today).days))
        msgs.append(f"«{p['name']}»: {soles(p['target_cents'])} para el {_fmt_day(dl)}, separando {soles(per_day)} al día")
    d.status = "confirmado"
    eng.db.commit()
    out = ("✓ ¡Meta guardada! " + "; ".join(msgs) + ". Te cuento cómo vas en tu resumen de la noche. "
           "Para guardar plata en tu meta dime «guardé 10 para mi meta».")
    if len(idx) == 1:   # una sola meta: además le armo el plan para llegar y qué producto financiero le ayuda
        try:
            from . import finance_coach
            p = props[idx[0]]
            tgt = amt or p["target_cents"]
            out += "\n\n" + finance_coach.goal_plan(eng.db, eng.b, name=p["name"], target_cents=tgt,
                                                    deadline=date.fromisoformat(p["deadline"]) if p.get("deadline") else None,
                                                    today=eng.today, kind=p["kind"])
        except Exception:   # noqa: BLE001
            pass
    return eng.resp(out)


def start(eng, ask_amount: bool = False) -> tuple[str, M.Draft | None, list[str]]:
    props = proposals(eng.db, eng.b, eng.today)
    if not props:
        return "", None, []
    d = eng.new_draft("propuesta_meta", {"props": props})
    eng.db.flush()
    return intro(props, ask_amount), d, quick(props)
