"""Coach financiero de Kallpa: de «quiero una meta» a un plan para llegar, con productos financieros.

Este módulo SOLO agrega respuestas; no guarda nada ni cambia datos. Se apoya en lo que Kallpa ya calcula:
la ganancia de los últimos 30 días, los fiados por cobrar, los consejos de `metrics.advice` y la cuota cómoda
de `report.capacity` (la misma del informe para el banco).

Qué hace:
- `goal_plan`: cuánto separar al día/semana, qué tan llevadero es, cómo llegar más rápido y qué producto financiero
  ayuda (ahorro, o crédito si la meta es para el negocio y la cuota le alcanza).
- `goals_coaching` / `goal_advice`: lo mismo para las metas que ya tiene activas («consejos para llegar a mi meta»).
- `consult`: respuesta a «¿me conviene un préstamo?», «necesito un crédito de 3000», «consejos financieros».
- `custom_goal_reply`: cuando Kallpa pregunta «¿de cuánto y para qué?» y la persona responde con su propia meta.

Todo es Python puro (sin librerías extra) para que también corra en la demo del navegador (Pyodide).
Las tasas son REFERENCIALES: cada entidad aplica la suya, y así se le dice a la persona.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any

from . import ledger, metrics, nlu
from .money import soles

TEA_REF = 0.40   # tasa efectiva anual referencial (la misma que usa el informe para el banco)

# --- para qué es la meta (decide si conviene hablar de crédito o solo de ahorro)
SAFETY_RE = r"\b(colchon|emergencia\w*|imprevisto\w*|fijos?|alquiler|deuda\w*|luz|gastos)\b"
STOCK_RE = r"\b(repon\w*|mercaderia|stock|inventario|surtir|surtido|campana)\b"
EQUIP_RE = r"\b(maquin\w*|equipo\w*|refri\w*|congel\w*|horno|cocina|licuadora|balanza|vitrina|moto|triciclo|carrito|local|puesto|freidora|cocinas?)\b"
GROWTH_RE = r"\b(compr\w*|abrir|ampli\w*|mejorar|negocio|crecer|vender mas)\b"

# --- pedir consejos para llegar a una meta
ADVICE_RE = (r"\b(consejos?|tips|recomienda\w*|aconsej\w*|ayudame|ayuda para|como llego|como lograr\w*|como logro|como alcanzo|"
             r"como puedo llegar|que hago para|llegar a mi meta|alcanzar mi meta|cumplir mi meta)\b")

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _fmt_day(d: date) -> str:
    return f"{d.day} de {MESES[d.month - 1]}"


def wants_advice(raw: str) -> bool:
    """¿La persona está pidiendo consejos (no solo diciendo que quiere una meta)?"""
    return re.search(ADVICE_RE, nlu.normalize(raw)) is not None


def purpose(name: str | None, kind: str | None = None) -> str:
    """seguridad | mercaderia | equipo | crecer | otro"""
    t = nlu.normalize(name or "")
    if re.search(SAFETY_RE, t):
        return "seguridad"
    if kind == "reposicion" or re.search(STOCK_RE, t):
        return "mercaderia"
    if re.search(EQUIP_RE, t):
        return "equipo"
    if re.search(GROWTH_RE, t):
        return "crecer"
    return "otro"


# ------------------------------------------------------------------ números

def _i_month() -> float:
    return (1 + TEA_REF) ** (1 / 12) - 1


def installment(amount_cents: int, months: int) -> int:
    """Cuota mensual referencial de un préstamo (cuotas fijas, TEA referencial)."""
    i = _i_month()
    return round(amount_cents * i / (1 - (1 + i) ** -months))


def max_loan(installment_cents: int, months: int) -> int:
    """Cuánto podría pedir con una cuota máxima (redondeado a S/ 100)."""
    i = _i_month()
    return int(round(installment_cents * (1 - (1 + i) ** -months) / i / 10000.0)) * 10000


def _profit30(db, b, today: date) -> int:
    try:
        return metrics.period_stats(db, b, today - timedelta(days=30), today + timedelta(days=1))["profit_cents"]
    except Exception:   # noqa: BLE001 (sin datos todavía)
        return 0


def _capacity(db, b) -> dict[str, int] | None:
    """Cuota cómoda disponible (ya descontadas sus deudas actuales), o None si aún faltan meses de datos."""
    try:
        from .report import capacity
        cap = capacity(db, b)
        sug = cap.get("suggested_max_installment_cents")
        if not sug:
            return None
        return {"avail": max(0, sug - (cap.get("current_debt_installments_cents") or 0)),
                "avg_net": cap.get("avg_net_cents") or 0}
    except Exception:   # noqa: BLE001
        return None


def _boosters(db, b, remaining: int, use_tips: bool = True) -> list[str]:
    """Formas concretas de llegar antes, con sus números."""
    out: list[str] = []
    try:
        rec = sum(r["pending_cents"] for r in ledger.receivables(db, b.id))
        if rec >= 3000 and remaining > 0:
            out.append(f"Cobra tus fiados: tienes {soles(rec)} por cobrar, eso cubre el {min(100, round(100 * rec / remaining))}% de tu meta.")
    except Exception:   # noqa: BLE001
        pass
    if use_tips:
        try:
            for tip in metrics.advice(db, b):
                if tip.startswith("Sigue mandando") or "fiados ya suman" in tip:
                    continue
                out.append(tip)
                if len(out) >= 3:
                    break
        except Exception:   # noqa: BLE001
            pass
    if len(out) < 3:
        out.append("Separa tu parte apenas cierres el día, antes de gastar en otra cosa: lo que sobra rara vez alcanza.")
    return out[:3]


# ------------------------------------------------------------------ productos financieros

def financing_text(db, b, *, name: str | None, kind: str | None = None, remaining: int | None = None,
                   level: str = "sin_datos", force_credit: bool = False) -> str:
    """Qué producto financiero le sirve: ahorro siempre; crédito solo si la meta es para el negocio (o lo pidió)
    y su cuota cómoda alcanza. Sin meta definida (name=None) da la visión general y le pide más datos."""
    pur = purpose(name, kind)
    general = name is None and not force_credit   # «consejos financieros» sin meta: todavía no sabemos para qué
    cap = _capacity(db, b)
    out = ["🏦 Productos financieros que te pueden ayudar:"]
    out.append("• Ahorro: abre una cuenta de ahorros aparte para tu meta (muchas cajas municipales y bancos tienen ahorro programado "
               "con descuento automático) o haz una «junta» con gente de confianza. Así no la gastas sin querer.")
    donde = ("• Dónde: cajas municipales y rurales, Mibanco, financieras y bancos tienen créditos para MYPE (si vendes productos del "
             "campo, mira Agrobanco). Escribe «informe» y te doy tu informe con ingresos verificados por Yape para mostrarle al banco.")
    cuidado = ("• Cuidado: evita el «gota a gota» y a prestamistas informales, cobran muchísimo más. Elige entidades supervisadas por la SBS "
               "y compara la TCEA (costo total), no solo la tasa.")
    wants_credit = force_credit or pur in ("mercaderia", "equipo", "crecer") or (pur == "otro" and level in ("exigente", "pesado"))
    if general:
        out.append("• Crédito: si lo quieres para tu negocio (mercadería, un equipo, ampliar), un crédito MYPE puede ayudarte; si es algo "
                   "personal o de emergencia, mejor ahorrar. Dime de cuánto y para qué y te digo si te conviene.")
        out += [donde, cuidado]
    elif pur == "seguridad" and not force_credit:
        out.append("• Un préstamo no te conviene para esto: es plata para tu tranquilidad, mejor ahorrarla de a poquito.")
    elif wants_credit:
        tipo = {"mercaderia": "un crédito de capital de trabajo (para comprar mercadería)",
                "equipo": "un crédito para equipo o activo fijo (para comprar tu equipo)"}.get(pur, "un crédito para MYPE (capital de trabajo)")
        if cap and cap["avail"] > 0:
            if remaining:
                cuota = installment(remaining, 12)
                if cuota <= cap["avail"]:
                    cuota_txt = (f" Pedir {soles(remaining)} a 12 meses te saldría en unos {soles(cuota)} al mes y tu cuota cómoda llega a "
                                 f"{soles(cap['avail'])}: te alcanza ✅.")
                else:
                    cuota_txt = (f" Pedir {soles(remaining)} a 12 meses sería unos {soles(cuota)} al mes y tu cuota cómoda llega a "
                                 f"{soles(cap['avail'])}: se pasa ⚠️. Con tu cuota podrías pedir unos {soles(max_loan(cap['avail'], 12))} y juntar "
                                 "el resto tú misma, o alargar el plazo.")
            else:
                cuota_txt = (f" Según tus números, tu cuota cómoda llega a {soles(cap['avail'])} al mes: a 12 meses podrías pedir "
                             f"unos {soles(max_loan(cap['avail'], 12))}.")
            cuota_txt += " (Cálculo referencial con 40% de TEA; cada entidad aplica la suya.)"
        else:
            cuota_txt = " Aún no tengo meses suficientes registrados para decirte qué cuota te conviene: sigue anotando tus ventas."
        if level == "ok" and not force_credit:
            out.append(f"• Crédito: como puedes juntarlo ahorrando, úsalo solo si te urge o quieres crecer ya. Si lo hicieras, sería {tipo}.{cuota_txt}")
        else:
            out.append(f"• Crédito: te serviría {tipo}.{cuota_txt}")
        out += [donde, cuidado]
    else:
        out.append("• Como es algo personal, mejor ahorrarlo que endeudarte. Si igual necesitas financiarlo, compara la TCEA (costo total) "
                   "entre entidades supervisadas por la SBS y evita el «gota a gota».")
    out.append("(Es una orientación general: confirma las condiciones con la entidad antes de firmar.)")
    return "\n".join(out)


# ------------------------------------------------------------------ plan de la meta

def goal_plan(db, b, *, name: str, target_cents: int, deadline: date | None, today: date, saved_cents: int = 0,
              kind: str | None = "ahorro", financing: bool = True, use_tips: bool = True) -> str:
    """Plan para llegar: ritmo, qué tan llevadero es, cómo acelerar y productos financieros."""
    remaining = max(0, int(target_cents) - int(saved_cents or 0))
    if remaining <= 0:
        return f"🎉 ¡Ya juntaste lo de «{name}»! Cuando quieras, ponte otra meta."
    profit30 = _profit30(db, b, today)
    level = "sin_datos"
    lines = [f"📌 Tu plan para «{name}» (te faltan {soles(remaining)}):"]
    if deadline and deadline > today:
        days = (deadline - today).days
        per_day = round(remaining / days)
        lines.append(f"• Separa {soles(per_day)} al día ({soles(round(remaining / days * 7))} por semana) y llegas el {_fmt_day(deadline)}.")
        if profit30 > 0:
            share = round(100 * (remaining / days * 30) / profit30)
            if share > 100:
                level = "pesado"
                lines.append(f"• Solo con ahorro no alcanza: necesitarías más de lo que ganaste en los últimos 30 días ({soles(profit30)}). "
                             "Conviene alargar el plazo o apoyarte en un financiamiento.")
            elif share <= 30:
                level = "ok"
                lines.append(f"• Es llevadero: sería el {share}% de lo que ganas al mes.")
            elif share <= 60:
                level = "exigente"
                lines.append(f"• Es exigente: sería el {share}% de lo que ganas al mes. Si puedes, alarga un poco el plazo o apóyate en lo de abajo.")
            else:
                level = "pesado"
                lines.append(f"• Solo con ahorro es muy pesado: sería el {share}% de lo que ganas al mes. Conviene alargar el plazo o apoyarte en un financiamiento.")
    elif profit30 > 0:
        monthly = max(1000, round(profit30 * 0.25))
        months = max(1, math.ceil(remaining / monthly))
        level = "ok" if months <= 3 else "exigente" if months <= 8 else "pesado"
        if months > 24:
            lines.append(f"• Con tu ritmo actual (ganaste {soles(profit30)} en los últimos 30 días) tardarías más de 2 años: conviene subir tus "
                         "ventas, ponerte un plazo realista o apoyarte en un financiamiento.")
        else:
            lines.append(f"• Si apartas 1 de cada 4 soles que ganas (unos {soles(monthly)} al mes), llegas en {months} {'mes' if months == 1 else 'meses'}.")
        lines.append("• Dime para cuándo la quieres (ej: «quiero juntar 1500 para una congeladora en 3 meses») y te calculo cuánto separar al día.")
    else:
        lines.append("• Aún no tengo ventas suficientes para calcular tu ritmo. Anota tus ventas unos días y te armo el plan con tus números.")
    lines.append("💡 Para llegar más rápido:")
    lines.extend(f"• {x}" for x in _boosters(db, b, remaining, use_tips=use_tips))
    txt = "\n".join(lines)
    if financing:
        txt += "\n\n" + financing_text(db, b, name=name, kind=kind, remaining=remaining, level=level)
    return txt


def goals_coaching(db, b, today: date, *, financing: bool = True, use_tips: bool = True) -> str | None:
    """Plan para la primera meta activa de la persona (None si no tiene)."""
    gs = [g for g in metrics.goals(db, b) if g["status"] == "activa"]
    if not gs:
        return None
    g = gs[0]
    saved = g.get("saved_cents") or 0
    if g.get("missing_cents") is not None:
        saved = g["target_cents"] - g["missing_cents"]
    dl = date.fromisoformat(g["deadline"]) if g.get("deadline") else None
    return goal_plan(db, b, name=g["name"], target_cents=g["target_cents"], deadline=dl, today=today,
                     saved_cents=saved, kind=g.get("kind"), financing=financing, use_tips=use_tips)


def goal_advice(db, b, today: date) -> str | None:
    """«Consejos para llegar a mi meta»: el plan de su meta activa. None si no tiene meta (se le pregunta de cuánto)."""
    return goals_coaching(db, b, today)


NO_GOAL_PROMPT = ("🎯 ¿Quieres llegar a algo concreto? Dime de cuánto y para qué, por ejemplo «quiero juntar 1500 para una "
                  "congeladora en 3 meses», y te armo el plan con tus números. Si piensas en un préstamo, pregúntame «¿me conviene un préstamo?».")


def consejos_footer(db, b, today: date) -> str:
    """Se agrega al final de «consejos»: su meta con plan, o la invitación a ponerse una."""
    try:
        plan = goals_coaching(db, b, today, use_tips=False)
        return "\n\n" + (plan if plan else NO_GOAL_PROMPT)
    except Exception:   # noqa: BLE001 (nunca romper la respuesta de siempre)
        return ""


# ------------------------------------------------------------------ «¿me conviene un préstamo?»

LOAN_RE = nlu.LOAN_RE


def consult(db, b, text: str, today: date) -> str:
    """Consejos financieros / préstamos con SUS números. Si no dijo de cuánto ni para qué, se lo pregunta."""
    t = nlu.normalize(text)
    amts = nlu.find_amounts(t)
    amount = max((a.cents for a in amts), default=None) if amts else None
    explicit_loan = re.search(LOAN_RE, t) is not None
    profit30 = _profit30(db, b, today)
    parts: list[str] = []
    if explicit_loan:
        if amount:
            parts.append(f"Veamos si te conviene pedir {soles(amount)} 👇")
        else:
            parts.append("Con gusto te oriento 💪. Para decirte cuánto te conviene necesito saber: ¿de cuánto sería y para qué lo usarías? "
                         "Por ejemplo: «necesito un préstamo de 3000 para surtir mi puesto».")
        parts.append(financing_text(db, b, name=text, remaining=amount, force_credit=True))
        if profit30 > 0:
            parts.append(f"Para ubicarte: en los últimos 30 días ganaste {soles(profit30)}.")
    else:   # «consejos financieros» en general
        try:
            parts.append(" ".join(metrics.advice(db, b)[:2]))
        except Exception:   # noqa: BLE001
            pass
        plan = goals_coaching(db, b, today, use_tips=False)
        if plan:
            parts.append(plan)
        else:
            parts.append(NO_GOAL_PROMPT)
            parts.append(financing_text(db, b, name=None, remaining=None, level="sin_datos"))
    return "\n\n".join(p for p in parts if p)


# ------------------------------------------------------------------ respuesta con su propia meta

_ORDINAL_RE = r"(^\s*(?:la |el |opcion |numero |meta )?[123]\s*$)|\b(?:la|el|opcion|numero|meta)\s*[123]\b|\b(?:primera|segunda|tercera|primero|segundo|tercero)\b"


def custom_goal_fields(raw: str, today: date) -> dict[str, Any] | None:
    """Detecta «1500 para una refrigeradora en 3 meses» o solo «800» como respuesta a «¿de cuánto y para qué?».
    Devuelve los campos de la meta, o None si en realidad eligió una de las propuestas (la 1, la 2, la 3)."""
    t = nlu.normalize(raw)
    if not t or re.search(_ORDINAL_RE, t):
        return None
    amts = nlu.find_amounts(t) or nlu._bare_amounts(t)
    if not amts:
        return None
    target = max(a.cents for a in amts)
    if target < 5000:   # menos de S/ 50: casi seguro es un número de opción, no una meta
        return None
    bare = re.fullmatch(r"(?:s/ ?)?\d+(?:\.\d+)?(?: soles)?", t.strip()) is not None
    if not bare and not re.search(r"\b(para|juntar|ahorrar|meta|en \d+ (?:semanas|meses|dias))\b", t):
        return None
    f = nlu._goal_fields(t, today)
    f["target_cents"] = target
    f["ask_purpose"] = not f.get("name")
    return f
