"""Agente de IA (integración del agente de Women2: github.com/maydelithzuniga/Women2).

Es el mismo cerebro para el chat de la app y para WhatsApp. Acepta texto o audio:
- Gemini (multimodal) escucha/lee el mensaje con el contexto del negocio y devuelve un JSON con la intención
  (mismo formato que `ai_advisor.py` de Women2, ampliado con gasto, pérdida, cobro de fiado, retiro y cierre).
- La intención NO se guarda directo: pasa por el motor de Kallpa (conversation.Engine), que valida, pregunta lo
  que falta, evita duplicados y actualiza caja e inventario. Así app y WhatsApp usan una sola fuente de datos.
- Si no hay clave de Gemini o falla, Kallpa sigue funcionando con sus reglas locales (y Whisper para audio).
- Respuesta por voz con Edge-TTS (voz es-PE-CamilaNeural), como en Women2, para enviarla por WhatsApp.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import timedelta
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """Eres el Asesor Financiero e Inteligencia de Negocio para una MYPE peruana. Hablas como una amiga que sabe de números.

CONTEXTO ACTUAL DEL NEGOCIO:
{negocio_metrics_json}

INSTRUCCIONES DE CLASIFICACIÓN (audio o texto):
Analiza el mensaje de la emprendedora y determina si se trata de:
1. VENTA: "Vendí [cantidad] [producto] a [monto] soles en [yape/efectivo]". Puede nombrar varios productos.
   Las cantidades pueden ser kilos, medio kilo, un cuarto, jabas, sacos, atados o "3 soles de tomate".
   Si dice que le quedaron debiendo o "fiado", marca fiado=true y la persona.
2. COMPRA: "Compré [cantidad] [producto] a [monto] soles en [yape/efectivo] para inventario".
3. GASTO: pagos del negocio (luz, alquiler, pasajes, bolsas, gas, sueldos).
4. RETIRO: plata que sacó del negocio para la casa.
5. PERDIDA: algo que se malogró, venció, rompió, se perdió, o plata robada/billete falso.
6. COBRO_FIADO: un cliente le pagó lo que debía.
7. META: "He separado/depositado [monto] soles para la meta [nombre]".
8. CIERRE: quiere cerrar su día o contar su plata.
9. CONSULTA: preguntas sobre su negocio, finanzas o recomendaciones (usa el contexto y sus números).

Usa los nombres de productos del contexto cuando coincidan. No inventes montos: si no los dijo, usa null.

REGLAS PARA METAS, CONSEJOS Y PRODUCTOS FINANCIEROS (cuando el tipo sea "meta" o "consulta"):
- Si quiere ponerse una meta y NO dijo de cuánto o para qué, NO inventes el monto: en respuesta_voz pregúntale de cuánto sería, para qué y para cuándo.
- Si ya hay monto: con sus números del contexto (ganancia, caja, fiados, metas) dile cuánto separar al día o a la semana y qué parte de su ganancia del mes es.
- Dale 2 o 3 consejos concretos para llegar a su meta o mejorar el negocio, usando sus productos, fiados, pérdidas y márgenes del contexto.
- Recomienda productos financieros según el caso: ahorro (cuenta de ahorros aparte, ahorro programado, junta) para metas personales o de seguridad; crédito de capital de trabajo o crédito MYPE (cajas municipales y rurales, Mibanco, financieras, bancos) solo si la meta es para el negocio y la cuota cabe en capacidad_credito del contexto. Nunca recomiendes un préstamo para el colchón de emergencias ni para los gastos fijos.
- No inventes tasas ni cuotas: usa solo capacidad_credito del contexto, di que cada entidad define su tasa y que compare la TCEA. Advierte contra el gota a gota y los prestamistas informales, y sugiere entidades supervisadas por la SBS.
- Habla simple y amable, en dos párrafos cortos como máximo.

FORMATO DE RESPUESTA OBLIGATORIO (JSON ESTRICTO):
{{
  "transcripcion": "lo que dijo la emprendedora, palabra por palabra",
  "intent": {{
    "tipo": "venta | compra | gasto | retiro | perdida | cobro_fiado | meta | cierre | consulta",
    "items": [{{"producto": "nombre o null", "cantidad": 1, "unidad": "kg | unidad | jaba | saco | atado | soles | null", "monto": 0.0}}],
    "monto": 0.0,
    "metodo_pago": "yape | efectivo | null",
    "persona": "nombre o null",
    "fiado": false,
    "nombre_meta": "nombre de la meta o null",
    "descripcion": "detalle corto o null"
  }},
  "respuesta_voz": "Texto empático y directo (máximo 2 párrafos) para responder en voz alta. Si es consulta, responde con sus números."
}}
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcripcion": {"type": "STRING"},
        "intent": {"type": "OBJECT", "properties": {
            "tipo": {"type": "STRING", "enum": ["venta", "compra", "gasto", "retiro", "perdida", "cobro_fiado", "meta", "cierre", "consulta"]},
            "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "producto": {"type": "STRING", "nullable": True}, "cantidad": {"type": "NUMBER", "nullable": True},
                "unidad": {"type": "STRING", "nullable": True}, "monto": {"type": "NUMBER", "nullable": True}}}},
            "monto": {"type": "NUMBER", "nullable": True},
            "metodo_pago": {"type": "STRING", "nullable": True},
            "persona": {"type": "STRING", "nullable": True},
            "fiado": {"type": "BOOLEAN", "nullable": True},
            "nombre_meta": {"type": "STRING", "nullable": True},
            "descripcion": {"type": "STRING", "nullable": True}},
            "required": ["tipo"]},
        "respuesta_voz": {"type": "STRING"},
    },
    "required": ["intent", "respuesta_voz"],
}


class AgentUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return settings.agent_enabled and bool(settings.gemini_api_key)


def business_context(db, b) -> dict[str, Any]:
    """Equivalente a get_business_metrics() de Women2, con los datos de Kallpa."""
    from . import ledger, metrics, units
    from .timeutil import today_lima
    today = today_lima()
    day = metrics.day_stats(db, b, today)
    month = metrics.period_stats(db, b, today.replace(day=1), today + timedelta(days=1))
    cash = metrics.cash_position(db, b)
    goals = [g for g in metrics.goals(db, b) if g["status"] == "activa"]
    prods = []
    for i in metrics.inventory(db, b)[:40]:
        prods.append({"nombre": i["name"], "unidad": i["unit"], "precio_venta": (i["price_cents"] or 0) / 100 or None,
                      "precio_compra": (i["cost_cents"] or 0) / 100 or None, "stock_actual": i["expected_stock"],
                      "se_vende": i.get("spec_text", {}).get("vende"), "se_compra": i.get("spec_text", {}).get("compra")})
    ctx = {
        "negocio": {"id": b.id, "nombre": b.name, "tipo": b.kind, "duena": b.owner_name},
        "productos": prods,
        "ventas": {"hoy": day["sales_cents"] / 100, "mes_actual": month["sales_cents"] / 100},
        "ganancia": {"hoy": day["profit_cents"] / 100, "mes_actual": month["profit_cents"] / 100},
        "caja": {"efectivo": cash["efectivo_cents"] / 100, "yape": cash["yape_cents"] / 100, "completa": cash["complete"]},
        "fiados_por_cobrar": [{"persona": r["counterparty"], "monto": r["pending_cents"] / 100} for r in ledger.receivables(db, b.id)][:10],
        "metas": [{"nombre": g["name"], "avance_pct": g.get("progress_pct"), "meta": g["target_cents"] / 100,
                   "plazo": g.get("deadline"), "falta": (g.get("missing_cents") or 0) / 100} for g in goals],
        "unidades_validas": sorted(units.UNIT_NAMES),
    }
    try:   # para que la IA aconseje con números reales: ganancia de 30 días y cuota cómoda (None = aún faltan meses de datos)
        from . import finance_coach
        cap = finance_coach._capacity(db, b)
        ctx["ganancia_ultimos_30_dias"] = finance_coach._profit30(db, b, today) / 100
        ctx["capacidad_credito"] = {"cuota_comoda_mensual": cap["avail"] / 100 if cap else None,
                                    "nota": "referencial; cada entidad define su tasa"}
    except Exception:   # noqa: BLE001 (la IA sigue funcionando sin esto)
        pass
    return ctx


def analyze(db, b, *, text: str | None = None, audio: bytes | None = None, mime_type: str = "audio/ogg",
            timeout: float = 9.0) -> dict[str, Any]:
    """process_user_audio() de Women2, ahora para audio o texto. Devuelve {transcripcion, intent, respuesta_voz}."""
    if not enabled():
        raise AgentUnavailable("Agente de IA desactivado o sin GEMINI_API_KEY")
    ctx = json.dumps(business_context(db, b), ensure_ascii=False)
    system = SYSTEM_PROMPT_TEMPLATE.format(negocio_metrics_json=ctx)
    parts: list[dict[str, Any]] = []
    if audio is not None:
        parts.append({"inline_data": {"mime_type": mime_type, "data": base64.b64encode(audio).decode()}})
        parts.append({"text": "Escucha el audio, transcríbelo, clasifica la transacción en JSON y genera el texto de respuesta."})
    else:
        parts.append({"text": f"Mensaje de la emprendedora: «{text}». Clasifícalo en JSON y genera el texto de respuesta."})
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA, "temperature": 0.1},
    }
    if _thinking_ok and _thinking_ok[0]:   # misma forma de «pensar poco» que ya funcionó al transcribir
        body["generationConfig"]["thinkingConfig"] = _thinking_ok[0]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.agent_model}:generateContent"
    try:
        r = httpx.post(url, json=body, headers={"x-goog-api-key": settings.gemini_api_key}, timeout=timeout)
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(raw)
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
        log.warning("Agente IA falló: %s", e)
        raise AgentUnavailable(str(e)) from e
    data.setdefault("intent", {"tipo": "consulta"})
    data.setdefault("respuesta_voz", "")
    data["transcripcion"] = (data.get("transcripcion") or text or "").strip()
    return data


# Qué forma de «pensar poco» acepta el modelo (se descubre en la primera llamada y se recuerda).
_THINKING_OPTIONS = [{"thinkingLevel": "minimal"}, {"thinkingLevel": "low"}, {"thinkingBudget": 0}, None]
_thinking_ok: list[Any] = []

TRANSCRIBE_PROMPT = ("Transcribe exactamente lo que dice este audio en español de Perú. Es una emprendedora contando "
                     "ventas, gastos o preguntas de su negocio. Escribe los montos y cantidades con números "
                     "(«dos kilos» → «2 kilos», «tres soles cincuenta» → «3.50 soles»). Devuelve SOLO el texto, sin comillas.")


_GEN_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
_models_cache: dict[str, Any] = {"t": 0.0, "names": []}
_good_model: list[str] = []
_thinking_by_model: dict[str, Any] = {}


def list_models(timeout: float = 10.0) -> list[str]:
    """Modelos Gemini de la cuenta que sirven para generateContent (se guarda 1 hora)."""
    import time
    if _models_cache["names"] and time.time() - _models_cache["t"] < 3600:
        return _models_cache["names"]
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                  headers={"x-goog-api-key": settings.gemini_api_key}, timeout=timeout)
    r.raise_for_status()
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    _models_cache.update(t=time.time(), names=names)
    return names


def audio_model_candidates() -> list[str]:
    """Orden: el que ya funcionó, el elegido (AGENT_AUDIO_MODEL), el principal y luego los «flash-lite»/«flash» de la cuenta."""
    first = [*_good_model, os.getenv("AGENT_AUDIO_MODEL") or "", settings.agent_model]
    extra: list[str] = []
    try:
        names = list_models()
        lite = [n for n in names if "flash" in n and "lite" in n and "tts" not in n and "image" not in n]
        flash = [n for n in names if "flash" in n and "lite" not in n and "tts" not in n and "image" not in n
                 and "live" not in n and "thinking" not in n]
        extra = sorted(lite, reverse=True)[:2] + sorted(flash, reverse=True)[:2]
    except Exception as e:  # noqa: BLE001
        log.warning("No pude listar los modelos de Gemini: %s", e)
    out: list[str] = []
    for m in first + extra:
        if m and m not in out:
            out.append(m)
    return out[:4]


def _generate(model: str, parts: list[dict[str, Any]], timeout: float, max_tokens: int = 400) -> str:
    """Una llamada a Gemini «pensando poco». Prueba qué opción de pensamiento acepta el modelo y la recuerda."""
    options = [_thinking_by_model[model]] if model in _thinking_by_model else _THINKING_OPTIONS
    last = ""
    for th in options:
        gen: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_tokens}
        if th:
            gen["thinkingConfig"] = th
        r = httpx.post(_GEN_URL.format(model), json={"contents": [{"role": "user", "parts": parts}], "generationConfig": gen},
                       headers={"x-goog-api-key": settings.gemini_api_key}, timeout=timeout)
        if r.status_code == 400 and th is not None and model not in _thinking_by_model:
            last = r.text[:200]
            continue
        r.raise_for_status()
        _thinking_by_model[model] = th
        if not _thinking_ok:
            _thinking_ok.append(th)
        cand = r.json()["candidates"][0]
        return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    raise AgentUnavailable(f"{model} rechazó la configuración: {last}")


def transcribe_audio(audio: bytes, mime_type: str = "audio/ogg", timeout: float = 45.0) -> str:
    """Solo transcribe: luego las reglas de Kallpa entienden el texto igual que si lo hubiera escrito.
    Si un modelo no responde a tiempo, prueba con otro más liviano de la cuenta y se queda con el que funcione."""
    import time
    if not enabled():
        raise AgentUnavailable("Agente de IA desactivado o sin GEMINI_API_KEY")
    parts = [{"inline_data": {"mime_type": mime_type, "data": base64.b64encode(audio).decode()}}, {"text": TRANSCRIBE_PROMPT}]
    t0 = time.time()
    errors = []
    for model in audio_model_candidates():
        left = timeout - (time.time() - t0)
        if left < 4:
            break
        t1 = time.time()
        try:
            text = _generate(model, parts, timeout=min(18.0, left))
            log.warning("Gemini transcribió en %.1f s con %s", time.time() - t1, model)
            if model not in _good_model:
                _good_model[:] = [model]
            return text.strip().strip("«»\"")
        except (httpx.HTTPError, KeyError, IndexError, AgentUnavailable) as e:
            log.warning("Gemini (%s) no pudo transcribir en %.1f s: %s", model, time.time() - t1, str(e)[:200])
            errors.append(f"{model}: {str(e)[:80]}")
            if _good_model == [model]:
                _good_model.clear()
    raise AgentUnavailable("; ".join(errors) or "sin modelos disponibles")


def diagnose() -> dict[str, Any]:
    """Prueba la clave y la velocidad de cada modelo candidato con un texto corto (para /api/diag/gemini)."""
    import time
    out: dict[str, Any] = {"enabled": enabled(), "agent_model": settings.agent_model,
                           "audio_model_env": os.getenv("AGENT_AUDIO_MODEL") or None, "tests": []}
    if not enabled():
        return out
    try:
        names = list_models()
        out["models_available"] = [n for n in names if "gemini" in n][:40]
    except Exception as e:  # noqa: BLE001
        out["models_error"] = str(e)[:300]
    for m in audio_model_candidates():
        t = time.time()
        try:
            txt = _generate(m, [{"text": "Responde solo con la palabra: hola"}], timeout=20, max_tokens=20)
            out["tests"].append({"model": m, "ok": True, "seconds": round(time.time() - t, 1), "reply": txt[:40],
                                 "thinking": _thinking_by_model.get(m)})
        except Exception as e:  # noqa: BLE001
            out["tests"].append({"model": m, "ok": False, "seconds": round(time.time() - t, 1), "error": str(e)[:300]})
    return out


async def text_to_speech_edge(text: str, output_path: str) -> str:
    """text_to_speech_edge() de Women2: voz peruana con Edge-TTS (gratis)."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=os.getenv("EDGE_TTS_VOICE", "es-PE-CamilaNeural"))
    await communicate.save(output_path)
    return output_path
