"""Interpretación del dictado en español de Perú con reglas (sin IA externa).

Convierte una frase como «vendí tres polos por cincuenta soles en efectivo» en un
borrador estructurado. Las cantidades y montos pueden venir en números o palabras.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# ---------------------------------------------------------------- normalización

UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21,
    "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
    "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29,
}
TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
        "ochenta": 80, "noventa": 90}
HUNDREDS = {"cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200,
            "trescientos": 300, "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400,
            "quinientos": 500, "quinientas": 500, "seiscientos": 600, "seiscientas": 600,
            "setecientos": 700, "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
            "novecientos": 900, "novecientas": 900}
NUMBER_WORDS = set(UNITS) | set(TENS) | set(HUNDREDS) | {"mil", "y"}


def strip_accents(text: str) -> str:
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.category(ch) == "Mn":
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out)).replace("ñ", "ñ")


def _words_to_numbers(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in NUMBER_WORDS and tok != "y":
            total = 0
            current = 0
            j = i
            consumed_any = False
            while j < len(tokens):
                t = tokens[j]
                if t in UNITS:
                    current += UNITS[t]
                elif t in TENS:
                    current += TENS[t]
                elif t in HUNDREDS:
                    current += HUNDREDS[t]
                elif t == "mil":
                    total += (current or 1) * 1000
                    current = 0
                elif t == "y" and consumed_any and j + 1 < len(tokens) and tokens[j + 1] in UNITS \
                        and tokens[j - 1] in TENS:
                    pass
                else:
                    break
                consumed_any = True
                j += 1
            value = total + current
            out.append(str(value))
            i = j
            continue
        out.append(tok)
        i += 1
    return out


def normalize(text: str) -> str:
    t = strip_accents((text or "").lower())
    t = t.replace("s/.", " s/ ").replace("s/", " s/ ")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)          # 1,140 -> 1140
    t = re.sub(r"(?<=\d),(?=\d{1,2}\b)", ".", t)       # 20,50 -> 20.50
    t = re.sub(r"c/u\b", " cada uno ", t)
    t = re.sub(r"[¿?¡!,;:\"()]", " ", t)
    t = re.sub(r"(?<!\d)\.(?!\d)", " ", t)
    t = re.sub(r"\bcada (uno|una)\b", "cada_uno", t)
    tokens = t.split()
    tokens = _words_to_numbers(tokens)
    t = " ".join(tokens).replace("cada_uno", "cada uno")
    t = re.sub(r"\bmedia docena\b", "6 unidades", t)
    t = re.sub(r"\b(\d+) docenas?\b", lambda m: f"{int(m.group(1)) * 12} unidades", t)
    t = re.sub(r"\buna? docena\b|\bdocena\b", "12 unidades", t)
    t = re.sub(r"\b(\d+) (?:soles |sol )?con (\d{2})(?: centimos| centavos)?\b", r"\1.\2 soles", t)
    t = re.sub(r"\b(\d{1,2}) (?:centimos|centavos|cntms)\b", lambda m: f"0.{int(m.group(1)):02d} soles", t)
    t = re.sub(r"\b(\d+) lucas?\b", r"\1 soles", t)
    # medidas de mercado: medio kilo, un cuarto, kilo y medio, media jaba
    t = re.sub(r"\bkg\b|\bkgs\b", "kilos", t)
    t = re.sub(r"\b(\d+) kilos? y medio\b", lambda m: f"{int(m.group(1))}.5 kilos", t)
    t = re.sub(r"\b(?:1 )?kilo y medio\b", "1.5 kilos", t)
    t = re.sub(r"\bmedio kilo\b|\bmedio(?= de )", "0.5 kilos", t)
    t = re.sub(r"\b3 cuartos?(?: de kilo| kilo)?\b", "0.75 kilos", t)
    t = re.sub(r"\b(?:1 )?cuarto(?: de kilo| kilo)?\b", "0.25 kilos", t)
    t = re.sub(r"\b(?:media|medio) (jaba|bolsa|docena|saco|caja|paquete|atado|bandeja)\b", r"0.5 \1", t)
    t = re.sub(r"\b1 kilo\b", "1 kilos", t)
    t = re.sub(r"\bun sol\b", "1 soles", t)
    return re.sub(r"\s+", " ", t).strip()


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("es") and word[-3] in "lnrdz":
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


# ---------------------------------------------------------------- métodos de pago

YAPE_RE = re.compile(r"\b(yape|yapeo|yapearon|yapeado|yapeada|yapeaste|yapee|yapear|plin|transferencia|tarjeta)\b")
CASH_RE = re.compile(r"\b(efectivo|cash|al contado|en mano|en billete|billetes|sencillo|en fisico)\b")


def find_method(t: str) -> str | None:
    y = YAPE_RE.search(t)
    c = CASH_RE.search(t)
    if y and c:
        return "mixto"
    if y:
        return "yape"
    if c:
        return "efectivo"
    return None


def find_pockets_transfer(t: str) -> tuple[str, str] | None:
    pocket = r"(yape|plin|efectivo|caja|cash|billetera)"
    m = re.search(rf"\bde(?:l|sde)? (?:mi |el |la )?{pocket}\b.*?\b(?:a|al|para) (?:mi |el |la )?{pocket}\b", t)
    if m:
        a = "yape" if m.group(1) in ("yape", "plin", "billetera") else "efectivo"
        b = "yape" if m.group(2) in ("yape", "plin", "billetera") else "efectivo"
        if a != b:
            return a, b
    return None


# ---------------------------------------------------------------- montos y cantidades

MONEY_SUFFIX = r"(?:soles|sol|nuevos soles)"


@dataclass
class Amount:
    cents: int
    start: int
    end: int
    explicit: bool       # tenía S/ o "soles"
    prefix: str          # palabras previas


def _parse_num(s: str) -> float:
    return float(s)


def find_amounts(t: str) -> list[Amount]:
    found: list[Amount] = []
    for m in re.finditer(rf"(?:s/ ?)?(\d+(?:\.\d{{1,2}})?)(?: ?{MONEY_SUFFIX}\b)?", t):
        num_txt = m.group(1)
        whole = m.group(0)
        explicit = whole.strip().startswith("s/") or bool(re.search(MONEY_SUFFIX + r"\s*$", whole))
        # lo que sigue
        after = t[m.end():m.end() + 30].strip()
        before = t[max(0, m.start() - 40):m.start()].strip()
        if not explicit:
            nxt = after.split(" ")[0] if after else ""
            # es cantidad si va seguido de unidades o de una palabra que no sea preposición de dinero
            if nxt in UNIT_WORDS or (nxt and nxt not in MONEY_FOLLOWERS):
                continue
            prev = before.split(" ")[-1] if before else ""
            prev2 = " ".join(before.split(" ")[-2:]) if before else ""
            if prev not in MONEY_PRECEDERS and prev2 not in MONEY_PRECEDERS:
                continue
        found.append(Amount(int(round(_parse_num(num_txt) * 100)), m.start(), m.end(), explicit, before))
    return found


UNIT_WORDS = {"unidades", "unidad", "kilos", "kilo", "atado", "atados", "jaba", "jabas", "manojo", "manojos", "kg", "litros", "litro", "platos", "plato",
              "docenas", "paquetes", "paquete", "cajas", "bolsas", "sacos", "saco", "pares", "par",
              "piezas", "metros", "metro", "botellas", "latas", "dias", "dia", "semanas", "meses",
              "veces", "por ciento", "%"}
MONEY_FOLLOWERS = {"", "en", "y", "por", "con", "de", "al", "a", "cada", "total", "el", "la", "que",
                   "pero", "me", "le", "mas", "del", "por el", "x", "para", "nomas", "no", "solo", "soles", "todo", "ya",
                   "mensual", "mensuales", "semanal", "diario", "diarios", "al mes"}
MONEY_PRECEDERS = {"a", "por", "de", "en", "pague", "pagaron", "gaste", "cobre", "debe", "dio", "pago",
                   "total", "cuesta", "costo", "vale", "precio", "con", "saque", "puse", "aporte",
                   "pase", "separe", "guarde", "ahorre", "aparte", "ahorrar", "juntar", "es", "son", "adelanto", "cuenta", "fie",
                   "me debe", "me dio", "me pago", "me pagaron", "le pague", "me cobraron", "tengo",
                   "hay", "conte", "invertir", "deposite", "transferi", "movi", "retire", "meta",
                   "unos", "unas", "como", "dejo", "quedo", "falta", "faltan", "cobro", "cobra",
                   "vendo", "compro", "necesito", "juntar", "cuota", "unos", "pagar", "sale", "salio", "salen", "cuestan", "valen", "gasto"}


def _qty(x: str) -> int | float:
    v = float(x)
    return int(v) if v.is_integer() else v


def find_quantity(t: str, product_spans: list[tuple[int, int]] | None = None) -> tuple[int | float | None, tuple[int, int] | None]:
    """Cantidad = número seguido de unidad o de un producto («0.5 kilos» = medio kilo, no 5)."""
    spans = product_spans or []
    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\b", t):
        after = t[m.end():].lstrip()
        nxt = after.split(" ")[0] if after else ""
        if t[max(0, m.start() - 3):m.start()].strip().endswith("s/"):
            continue
        if re.match(MONEY_SUFFIX + r"\b", after):
            continue
        if nxt in UNIT_WORDS:
            return _qty(m.group(1)), (m.start(), m.end())
        for (ps, _pe) in spans:
            gap = t[m.end():ps]
            if 0 <= ps - m.end() <= 12 and re.fullmatch(r"\s*(?:de |del |de la |de los )?\s*", gap or " "):
                return _qty(m.group(1)), (m.start(), m.end())
    return None, None


# ---------------------------------------------------------------- productos

@dataclass
class ProductRef:
    id: int
    name: str
    aliases: list[str]
    price_cents: int | None = None
    cost_cents: int | None = None
    track_stock: bool = True
    is_input: bool = False
    is_service: bool = False


def product_terms(p: ProductRef) -> set[str]:
    terms: set[str] = set()
    base = normalize(p.name)
    terms.add(base)
    words = base.split()
    terms.add(" ".join(w + "s" if not w.endswith("s") else w for w in words[:1]) + (" " + " ".join(words[1:]) if len(words) > 1 else ""))
    if words:
        terms.add(words[0])
        terms.add(words[0] + "s")
        terms.add(words[0] + "es")
    for a in p.aliases or []:
        na = normalize(a)
        if na:
            terms.add(na)
            terms.add(na + "s")
    return {x.strip() for x in terms if x.strip()}


@dataclass
class ProductMatch:
    candidates: list[ProductRef]
    span: tuple[int, int] | None
    text: str | None = None


def match_products(t: str, products: list[ProductRef]) -> ProductMatch:
    best_len = 0
    best: list[tuple[ProductRef, tuple[int, int], str]] = []
    for p in products:
        if not p:
            continue
        p_best: tuple[int, tuple[int, int], str] | None = None
        for term in product_terms(p):
            for m in re.finditer(rf"\b{re.escape(term)}\b", t):
                L = len(term)
                if p_best is None or L > p_best[0]:
                    p_best = (L, (m.start(), m.end()), term)
        if p_best:
            if p_best[0] > best_len:
                best_len = p_best[0]
                best = [(p, p_best[1], p_best[2])]
            elif p_best[0] == best_len:
                best.append((p, p_best[1], p_best[2]))
    if not best:
        return ProductMatch([], None)
    # si un término más específico (ej. "polo estampado") contiene al genérico, gana el específico
    return ProductMatch([b[0] for b in best], best[0][1], best[0][2])


STOP = {"de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "a", "en", "por", "y",
        "con", "que", "me", "le", "se", "mi", "mis", "su", "sus", "al", "para", "hoy", "ayer",
        "soles", "sol", "efectivo", "yape", "total", "cada", "uno", "unidades", "unidad", "todo",
        "pague", "cobre", "pagaron", "pago", "vendi", "compre", "gaste", "nomas", "ya", "tambien",
        "kilos", "kilo", "litros", "litro", "plato", "platos", "s/", "lo", "les", "esta", "este"}


def guess_new_product(t: str, qty_span: tuple[int, int] | None) -> str | None:
    if not qty_span:
        return None
    rest = t[qty_span[1]:].split()
    words = []
    for w in rest:
        if w in ("de", "del") and not words:
            continue
        if w in UNIT_WORDS and not words:
            continue
        if w in STOP or re.match(r"^\d", w):
            break
        words.append(w)
        if len(words) == 2:
            break
    if not words:
        return None
    name = " ".join([singular(words[0])] + words[1:])
    return name


def extract_product_names(description: str) -> list[str]:
    """Del rubro: «vendo polos, shorts y gorras» -> [polos, shorts, gorras]."""
    t = normalize(description)
    m = re.search(r"\b(?:vendo|ofrezco|hago|preparo|trabajo con|vendemos|hacemos)\b (.+?)(?:\b(?:compro|cobro|me pagan|y cobro|y me pagan|los compro|lo compro|por yape|en efectivo)\b|$)", t)
    if not m:
        return []
    chunk = m.group(1)
    parts = re.split(r",| y | e | o ", chunk)
    names = []
    for p in parts:
        words = [w for w in p.split() if w not in STOP and not w.isdigit()]
        if words:
            names.append(" ".join(words[:2]))
    return [n for n in names if len(n) > 2][:8]


# ---------------------------------------------------------------- fechas y nombres

WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}


def find_day(t: str, today: date) -> date | None:
    if re.search(r"\banteayer\b", t):
        return today - timedelta(days=2)
    if re.search(r"\bayer\b", t):
        return today - timedelta(days=1)
    m = re.search(r"\bel (lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b", t)
    if m:
        wd = WEEKDAYS[m.group(1)]
        delta = (today.weekday() - wd) % 7 or 7
        return today - timedelta(days=delta)
    return None


NAME_STOP = STOP | {"senora", "senor", "sra", "sr", "don", "dona", "vecina", "vecino", "tia", "tio",
                    "amiga", "amigo", "cliente", "casera", "casero", "cuenta", "fiado", "debe",
                    "mitad", "resto", "casa", "proveedor", "proveedora", "banco", "caja", "quien", "quienes",
                    "tengo", "cobrar", "todo", "todos", "nadie", "alguien", "efectivo", "yape", "plin", "kilos", "kilo",
                    "soles", "sol", "unidades", "ella", "solo", "tambien", "ahora", "recien", "ayer", "hoy"}


def find_person(t: str) -> str | None:
    pats = [
        r"\b(?:a|de|para|con) (?:la |el |mi )?(?:senora|senor|sra|sr|don|dona|vecina|vecino|tia|tio|amiga|amigo|casera|casero|cliente) ([a-zñ]+)",
        r"\b(?:la |el )?(?:senora|senor|sra|sr|don|dona|vecina|vecino|tia|tio|casera|casero) ([a-zñ]+)",
        r"\bfie(?:\s\S+){0,5}? a (?:la |el )?([a-zñ]+)",
        r"^([a-zñ]{3,}) (?:se llevo|se lleva|se llevo|saco|pidio|me saco|me pidio)\b",
        r"\bfiado a (?:la |el )?(?:senora |senor |don |dona )?([a-zñ]{3,})",
        r"\ba ([a-zñ]{3,})(?= me debe| le fie| fiado| le quedo| quedo debiendo)",
        r"\bde ([a-zñ]{3,})(?= me debe| quedo debiendo| le quedo)",
        r"^([a-zñ]{3,}) me (?:pago|pagó|debe|devolvio|yapeo|cancelo)",
        r"\b([a-zñ]{3,}) me (?:pago|debe|quedo debiendo|yapeo)\b",
        r"\b(?:cobre|cobré) (?:a |el fiado de |lo de |la deuda de )(?:la |el )?([a-zñ]{3,})",
        r"\bfiado de (?:la |el )?(?:senora |senor |don |dona )?([a-zñ]{3,})",
    ]
    for p in pats:
        m = re.search(p, t)
        if m and m.group(1) not in NAME_STOP:
            return m.group(1).capitalize()
    return None


# ---------------------------------------------------------------- intención

@dataclass
class Parsed:
    intent: str
    fields: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    text: str = ""


CONFIRM_RE = re.compile(r"^(si|sip|claro|ya|ya pues|dale|ok|okay|okey|listo|correcto|guardalo|guarda|guardar|confirmo|confirmar|confirmado|de acuerdo|asi es|eso es|esta bien|perfecto|exacto|si guardalo|si guarda|si esta bien|si por favor|si correcto|si listo|ya guardalo|registralo|registrar)\b[\s\w]*$")
CANCEL_RE = re.compile(r"^(no|nop|cancela|cancelar|cancelalo|olvidalo|dejalo|mejor no|no guardes|no lo guardes|borra eso|no gracias)\b")
QUESTION_PATTERNS: list[tuple[str, str]] = [
    (r"\bque (es|significa|quiere decir)\b.*\bflujo\b|\bflujo de caja\b.*\?|\bexplica.*flujo\b", "explica_flujo"),
    (r"\bque (es|significa)\b.*\bganancia\b|\bexplica.*ganancia\b", "explica_ganancia"),
    (r"\bque (es|significa)\b.*\bmargen\b|\bexplica.*margen\b", "explica_margen"),
    (r"\bque (es|significa)\b.*\bequilibrio\b|\bexplica.*equilibrio\b|\bcuanto (tengo|necesito) que vender\b", "explica_equilibrio"),
    (r"\bque (es|significa)\b.*\brotacion\b|\bexplica.*rotacion\b", "explica_rotacion"),
    (r"\bque (es|significa)\b.*\bcaja\b|\bexplica.*caja\b", "explica_caja"),
    (r"\bque (es|significa)\b.*\bcapacidad de pago\b|\bcuota\b.*\b(puedo|podria)\b", "explica_cuota"),
    (r"\b(cuanto|que) puedo (usar|gastar)\b.*\breponer\b|\breponer\b|\breposicion\b|\bproxima compra\b", "reponer"),
    (r"\b(consejo|consejos|tips|recomienda|recomendacion|que hago|como mejoro|como vendo mas)\b", "consejos"),
    (r"\b(resumen de la semana|como me fue (en )?la semana|esta semana|semanal)\b", "resumen_semana"),
    (r"\b(resumen del mes|como me fue (en )?el mes|este mes|mensual)\b", "resumen_mes"),
    (r"\b(resumen|como me fue|como voy|como va|cuanto gane|cuanto vendi|mi dia|como estoy)\b", "resumen_dia"),
    (r"\b(cuanto cobre)\b", "resumen_dia"),
    (r"\b(cuanto tengo|cuanta plata tengo|mi caja|en caja|saldo)\b", "caja"),
    (r"\b(fiados|quien me debe|cuanto me deben|me deben)\b\??$", "fiados"),
    (r"\b(mis metas|como va mi meta|mi meta|cuanto me falta)\b|^(metas?|ver metas|proponme (una )?meta|que meta me pongo)$", "metas"),
    (r"\b(stock|inventario|mercaderia que tengo|que me queda|cuanto me queda)\b\??$", "inventario"),
    (r"^(?:y )?(?:cuant[oa]s?|que tanto) (?:de )?(?:[a-zñ]+ ){0,3}(?:me queda|me quedan|queda|quedan|tengo)\b", "inventario"),
    (r"\b(informe|reporte)\b", "informe"),
    (r"\b(ayuda|que puedo decir|como funciona|que haces|hola|buenas|buenos dias|buenas tardes|buenas noches)\b", "ayuda"),
]


def _has(t: str, pattern: str) -> bool:
    return re.search(pattern, t) is not None


EXPENSE_CATEGORIES: list[tuple[str, str, str]] = [
    (r"\balquiler|\bcuarto\b|\blocal\b|\bpuesto\b", "Alquiler", "fijo"),
    (r"\bluz\b|\bagua\b|\bservicios\b", "Luz y agua", "fijo"),
    (r"\bgas\b|\bbalon\b", "Gas", "fijo"),
    (r"\binternet\b|\bcelular\b|\brecarga\b|\bplan\b", "Internet y celular", "fijo"),
    (r"\bsueldo\b|\bayudante\b|\btrabajador|\bpersonal\b|\bjornal\b|\bpropina\b", "Sueldos", "fijo"),
    (r"\bpasaje|\bmovilidad\b|\btaxi\b|\bmototaxi\b|\bflete\b|\bdelivery\b|\bcarrera\b|\bcombustible\b|\bgasolina\b", "Movilidad y fletes", "variable"),
    (r"\bbolsa|\bempaque|\bdescartable|\btaper|\bcajita|\betiqueta", "Bolsas y empaques", "variable"),
    (r"\bpublicidad|\bvolante|\banuncio|\bfacebook\b|\btiktok\b", "Publicidad", "variable"),
    (r"\bmunicipalidad|\blicencia|\barbitrio|\bimpuesto|\bsunat\b", "Licencias e impuestos", "fijo"),
]
# Orientación financiera («¿me conviene un préstamo?», «necesito un crédito de 3000», «consejos financieros»)
LOAN_RE = r"\b(prestamos?|(?<!\ba )(?<!\bal )creditos?|financiar|financiamiento|capital de trabajo|endeudar\w*|pedir prestado|cuanto (?:puedo|podria) pedir)\b"
FINADV_RE = r"\b(consejos?|asesor\w*|orientacion|tips|ayuda)\s+(?:\w+\s+)?financier\w+"
LOAN_ASK_RE = (r"\b(me conviene|conviene|quiero|quisiera|necesito|puedo|podria|deberia|recomiendas?|aconsejas?|como|sacar|pedir|"
               r"solicitar|financiar|consejos?|opciones|dame|dime|que|cual|cuales)\b")
LOAN_DONE_RE = r"\b(pague|pago|pagamos|abone|cancele|gaste|vendi|compre|recibi|cobre|me prestaron|me dieron|se llevo|le fie)\b"
PERSONAL_WORDS = r"\b(casa|personal|mio|mia|hijo|hija|hijos|colegio|familia|mama|papa|esposo|pareja|medicina|doctor|comida de la casa|mis cosas|para mi|uso personal|mercado de la casa)\b"


def parse(text: str, products: list[ProductRef], today: date) -> Parsed:
    t = normalize(text)
    p = Parsed(intent="desconocido", text=t)
    if not t:
        return p

    day = find_day(t, today)
    if day:
        p.fields["day"] = day.isoformat()

    # --- deshacer lo último que se guardó
    if _has(t, r"^(deshacer|deshaz|deshazlo|deshaslo|borra eso|borralo|anula eso|anulalo|eso no era|me equivoque borra)\b"):
        p.intent = "deshacer"
        return p
    # --- cobrar un fiado (mensaje listo para la casera)
    if _has(t, r"^(cobrar|cobrale|cobrarle|a quien (le )?tengo que cobrar|a quienes tengo que cobrar|quien me debe|quienes me deben|lista de (cobros|fiados)|que me deben)\b"):
        p.intent = "cobrar"
        m = re.search(r"\b(?:a|de) (?:la |el |mi )?(?:senora |senor |vecina |vecino |don |dona |sra |sr )?([a-zñ]{3,})", t)
        if m and m.group(1) not in NAME_STOP:
            p.fields["counterparty"] = m.group(1).capitalize()
        return p
    # --- gasto que se paga cada cierto tiempo («pago 250 de alquiler al mes», «bolsas 15 a la semana»)
    from . import costs as _costs
    if _costs.find_frequency(t) and (_has(t, r"\b(pago|pagamos|gasto|gastamos|sale|cuesta|me cobran|doy)\b")
                                     or _costs.FIXED_RE.search(t) or _costs.VARIABLE_RE.search(t)) \
            and not _has(t, r"\b(ahorrar|juntar|meta|vendo|vendi|vendemos|gane|gano|me pagan|cobro)\b"):
        items = _costs.parse(text)
        if items:
            p.intent = "gasto_fijo"
            p.fields["items"] = items
            return p
    # --- contar la plata (en vez de «saldo inicial»)
    if _has(t, r"\b(contar (mi )?plata|contar (mi )?caja|cuenta mi plata|quiero contar)\b"):
        p.intent = "contar_plata"
        return p
    # --- cierre del día
    if _has(t, r"\b(cierre|cerrar (mi |el )?dia|cierro (mi |el )?dia|cerrar (la )?caja|cuadrar (mi |la )?caja|cuadre de caja|termine (mi |el )?dia|fin del dia|ya cerre)\b"):
        p.intent = "cierre"
        return p
    # --- corrección / anulación de lo ya guardado
    if _has(t, r"^(anula|anular|borra|borrar|elimina|eliminar|quita)\b.*\bultim[oa]\b|\banula(r)? (la|el) ultim"):
        p.intent = "anular"
        return p
    if _has(t, r"^(corrige|corregir|correccion|me equivoque|estaba mal|cambia|cambiar|arregla)\b"):
        p.intent = "corregir"
        p.fields.update(_value_fields(t, products))
        return p

    if CONFIRM_RE.match(t) and len(t.split()) <= 5:
        p.intent = "confirmar"
        return p
    if CANCEL_RE.match(t) and len(t.split()) <= 4 and not re.search(r"\d|yape|efectivo", t):
        p.intent = "cancelar"
        return p

    # --- consentimiento del informe
    if _has(t, r"\b(envia|enviar|manda|mandar|mandalo|envialo)\b.*\b(informe|reporte|bcp|banco)\b"):
        p.intent = "consentir_informe"
        return p

    # --- saldos iniciales
    if _has(t, r"\b(empiezo|empece|arranco|inicio|saldo inicial|para empezar|al empezar|comienzo)\b") and find_amounts(t):
        p.intent = "saldo_inicial"
        p.fields["balances"] = _balances(t)
        return p

    # --- catálogo: precio/costo/stock de un producto
    if _has(t, r"\b(cuesta|cuestan|precio|lo vendo|los vendo|la vendo|las vendo|vendo a|me cuesta|me cuestan|lo compro a|los compro a|costo|en stock|valen|vale)\b") \
            and not _has(t, r"^(vendi|compre|gaste|pague|me compraron)"):
        pm = match_products(t, products)
        p.intent = "catalogo"
        p.fields.update(_catalog_fields(t, pm))
        return p

    # --- orientación sobre préstamos y productos financieros (solo responde; no registra nada)
    if (_has(t, LOAN_RE) or _has(t, FINADV_RE)) and ("?" in (text or "") or _has(t, LOAN_ASK_RE)) \
            and not _has(t, LOAN_DONE_RE):
        p.intent = "consejos_financieros"
        return p
    # --- metas
    if _has(t, r"\b(separe|aparte|guarde|ahorre)\b") and find_amounts(t):
        p.intent = "ahorro_meta"
        amts = find_amounts(t)
        p.fields["amount_cents"] = amts[0].cents
        m = find_method(t)
        if m in ("efectivo", "yape"):
            p.fields["method"] = m
        gm = re.search(r"\bpara (?:la |el |mi |mis )?(.+)$", t)
        if gm:
            p.fields["goal_hint"] = gm.group(1)
        return p
    if _has(t, r"\b(quiero|quisiera|mi meta|meta|necesito juntar|quiero juntar|quiero ahorrar|ahorrar para|juntar para)\b") \
            and _has(t, r"\b(comprar|ahorrar|juntar|abrir|reponer|meta|campana|para)\b"):
        p.intent = "meta"
        p.fields.update(_goal_fields(t, today))
        return p

    # --- traslados
    tr = find_pockets_transfer(t)
    if tr and _has(t, r"\b(pase|pasé|transferi|movi|deposite|cambie|converti|saque|retire|mande|puse)\b"):
        amts = find_amounts(t) or _bare_amounts(t)
        p.intent = "movimiento"
        p.fields.update({"kind": "traslado", "method": tr[0], "to_method": tr[1]})
        if amts:
            p.fields["amount_cents"] = amts[0].cents
        return p

    # --- conteos (stock y caja)
    if _has(t, r"^(conte|cuento|tengo|me quedan|quedan|hay|en total hay|me queda)\b") and not _has(t, r"\b(stock)\b"):
        pm = match_products(t, products)
        qty, _ = find_quantity(t, [pm.span] if pm.span else [])
        if pm.candidates and qty is not None:
            p.intent = "conteo_stock"
            p.fields.update({"counted": qty, "product_candidates": [c.id for c in pm.candidates]})
            return p
        if _has(t, r"\b(caja|efectivo|yape|billetera|plata|soles)\b"):
            amts = find_amounts(t) or _bare_amounts(t)
            if amts:
                p.intent = "conteo_caja"
                m = find_method(t)
                p.fields.update({"counted_cents": amts[0].cents, "pocket": "yape" if m == "yape" else "efectivo"})
                return p

    # --- pérdidas de cualquier tipo (mercadería malograda, robo, rotura, vencimiento…)
    if _has(t, LOSS_RE):
        p.intent = "perdida"
        p.fields.update(loss_fields(t, products))
        return p
    if _has(t, r"\b(me devolvieron|devolvio|devolucion|devolvieron)\b"):
        pm = match_products(t, products)
        qty, _ = find_quantity(t, [pm.span] if pm.span else [])
        amts = find_amounts(t)
        p.intent = "movimiento"
        p.fields.update({"kind": "ajuste_stock", "reason": "devolucion", "quantity": qty or 1,
                         "product_candidates": [c.id for c in pm.candidates]})
        if amts:
            p.fields["amount_cents"] = amts[0].cents
            m = find_method(t)
            if m in ("efectivo", "yape"):
                p.fields["method"] = m
        return p

    # --- ventas explícitas (antes que cobros: «vendí... me pagó 30 y me debe 10»)
    if _has(t, r"^(ayer |hoy |el \w+ |en (esta|la) \w+ \w+ |en la (manana|tarde) |hasta ahora )?(vendi|vendimos|me compraron)\b") \
            or _has(t, r"^(en (esta|la) ultima hora|en la manana|en la tarde|hasta ahora|en lo que va)\b.*\bvend"):
        p.intent = "movimiento"
        items = parse_sale_items(t, products) if not _has(t, r"\b(fiado|fie|me debe)\b") else None
        if items:
            p.fields.update({"kind": "ventas", **items})
            return p
        p.fields["kind"] = "venta"
        p.fields.update(_sale_fields(t, products, p))
        return p

    # --- cobros de fiado
    if (_has(t, r"\b(me pago|me pagaron|cobre|me cancelo|me devolvio|me yapeo|me abono|pago su|pago lo|me trajo)\b")
            and _has(t, r"\b(fiado|debia|deuda|lo que me debe|lo que me debia|lo que debia|cuenta|me debe)\b")) \
            or _has(t, r"^[a-zñ]+ me (pago|cancelo|abono|yapeo)\b(?!.*\bpor\b)"):
        p.intent = "movimiento"
        p.fields["kind"] = "cobro_fiado"
        p.fields.update(_value_fields(t, products))
        p.fields["counterparty"] = find_person(t)
        return p

    # --- pago de deuda / cuota
    if _has(t, r"\b(pague|pago|abone|cancele)\b.*\b(cuota|prestamo|deuda|credito|banco|caja municipal|financiera)\b"):
        p.intent = "movimiento"
        p.fields["kind"] = "pago_deuda"
        p.fields.update(_value_fields(t, products))
        cm = re.search(r"\b(?:a la|al|a) (caja \w+|banco \w+|financiera \w+|[a-zñ]+bank|[a-zñ]+banco)\b", t)
        if cm:
            p.fields["counterparty"] = cm.group(1).title()
        return p

    # --- retiros personales
    if _has(t, r"\b(me saque|saque|retire|me lleve|tome|use|agarre|saque)\b") and _has(t, PERSONAL_WORDS):
        p.intent = "movimiento"
        p.fields["kind"] = "retiro_personal"
        p.fields.update(_value_fields(t, products))
        dm = re.search(r"\bpara (?:la |el |mi |mis )?(.+)$", t)
        p.fields["description"] = dm.group(1) if dm else "gastos de la casa"
        return p

    # --- aportes
    if _has(t, r"\b(puse|aporte|meti|agregue|inyecte|invertir|inverti|presto mi|preste al negocio)\b") \
            and _has(t, r"\b(bolsillo|mi plata|propio|propia|ahorro|ahorros|negocio|caja|de lo mio|de mi)\b"):
        p.intent = "movimiento"
        p.fields["kind"] = "aporte"
        p.fields.update(_value_fields(t, products))
        return p

    # --- ventas
    if _has(t, r"\b(vendi|vendimos|vendio|me compraron|despache|atendi|cobre|hice)\b") or _has(t, r"\b(le fie|fie|fiado)\b"):
        p.intent = "movimiento"
        items = parse_sale_items(t, products) if not _has(t, r"\b(fiado|fie|me debe)\b") else None
        if items:
            p.fields.update({"kind": "ventas", **items})
            return p
        p.fields["kind"] = "venta"
        p.fields.update(_sale_fields(t, products, p))
        return p

    # --- compras de mercadería
    if _has(t, r"\b(compre|compramos|repuse|me traje|traje|pague al proveedor|le compre|surti)\b") \
            or (_has(t, r"\b(gaste|pague|invertí)\b") and _has(t, r"\b(mercaderia|insumos|insumo|productos|material|materiales)\b")):
        p.intent = "movimiento"
        p.fields["kind"] = "compra"
        p.fields.update(_purchase_fields(t, products, p))
        return p

    # --- gastos
    if _has(t, r"\b(gaste|pague|pago de|pagamos|se fue|se gasto|me cobraron)\b"):
        p.intent = "movimiento"
        if _has(t, PERSONAL_WORDS):
            p.fields["kind"] = "retiro_personal"
            dm = re.search(r"\b(?:para|en) (?:la |el |mi |mis )?(.+)$", t)
            p.fields["description"] = dm.group(1) if dm else "gastos de la casa"
        else:
            p.fields["kind"] = "gasto"
            cat, typ = "Otros gastos", "variable"
            for pat, c, ty in EXPENSE_CATEGORIES:
                if _has(t, pat):
                    cat, typ = c, ty
                    break
            p.fields["category"] = cat
            p.fields["expense_type"] = typ
            dm = re.search(r"\b(?:en|de) (?:la |el |los |las )?([a-zñ ]+?)(?: \d| por| en | con|$)", t)
            p.fields["description"] = dm.group(1).strip() if dm else cat.lower()
        p.fields.update(_value_fields(t, products))
        return p

    # --- preguntas
    for pat, intent in QUESTION_PATTERNS:
        if _has(t, pat):
            p.intent = intent
            return p

    # --- respuesta suelta (para completar un borrador)
    p.intent = "valor"
    p.fields.update(_value_fields(t, products))
    return p


def _bare_amounts(t: str) -> list[Amount]:
    out = []
    for m in re.finditer(r"\b(\d+(?:\.\d{1,2})?)\b", t):
        out.append(Amount(int(round(float(m.group(1)) * 100)), m.start(), m.end(), False, t[:m.start()]))
    return out


def _value_fields(t: str, products: list[ProductRef]) -> dict[str, Any]:
    """Valores sueltos: método, monto, cantidad, producto, número de opción."""
    out: dict[str, Any] = {}
    m = find_method(t)
    if m:
        out["method"] = m
    pm = match_products(t, products)
    if pm.candidates:
        out["product_candidates"] = [c.id for c in pm.candidates]
    qty, qspan = find_quantity(t, [pm.span] if pm.span else [])
    if qty is not None:
        out["quantity"] = qty
    amts = [a for a in find_amounts(t) if not (qspan and a.start == qspan[0])]
    if not amts and qty is None:
        bare = _bare_amounts(t)
        if len(bare) == 1:
            out["bare_number"] = bare[0].cents
    roles = _payment_roles(t, amts)
    out.update(roles)
    if amts and "amount_cents" not in out and not roles:
        out["amount_cents"] = amts[-1].cents
    if re.search(r"\b(eran|fueron|son|era|fue)\b", t) and qty is None:
        bare = re.search(r"\b(?:eran|fueron|son)\s+(\d+)\b(?!\s*soles)", t)
        if bare and not amts:
            out["quantity"] = int(bare.group(1))
            out.pop("bare_number", None)
    if re.search(r"\b(total|en total)\b", t):
        out["price_role"] = "total"
    elif re.search(r"\b(cada uno|cada una|por unidad|la unidad|c/u|cada)\b", t):
        out["price_role"] = "unidad"
    if re.search(r"\b(precio de venta|lo que cobro|lo vendo|venta)\b", t):
        out["catalog_role"] = "precio"
    elif re.search(r"\b(costo|me cuesta|lo que pago|lo compro|compra)\b", t):
        out["catalog_role"] = "costo"
    om = re.match(r"^(?:el |la |opcion )?(\d)$|^(?:el |la )?(primero|primera|segundo|segunda|tercero|tercera)\b", t)
    if om:
        idx = om.group(1) or {"primero": "1", "primera": "1", "segundo": "2", "segunda": "2", "tercero": "3", "tercera": "3"}[om.group(2)]
        out["option_index"] = int(idx)
    person = find_person(t)
    if person:
        out["counterparty"] = person
    return out


def _payment_roles(t: str, amts: list[Amount]) -> dict[str, Any]:
    """Detecta pagos parciales y fiados: «me pagó 30 y me debe 20», «al fiado»."""
    out: dict[str, Any] = {}
    totals: list[int] = []
    for a in amts:
        pre = a.prefix[-25:]
        if re.search(r"(me debe|debe|quedo debiendo|queda debiendo|falta|faltan|me quedo debiendo|le fie)\s*$", pre):
            out["pending_cents"] = a.cents
        elif re.search(r"(me pago|me dio|me dejo|adelanto|a cuenta|dejo|me adelanto|pago|me yapeo|solo)\s*$", pre) \
                and not re.search(r"(vendi|compre)\b[^\d]*$", pre):
            out["paid_cents"] = a.cents
        elif re.search(r"(en total|total|por todo|por)\s*$", pre):
            totals.append(a.cents)
    if len(set(totals)) == 1:
        out["total_cents"] = totals[0]
    elif len(set(totals)) > 1:
        out["amount_options"] = totals
    if (re.search(r"\b(fiado|al fiado|le fie|fie|me debe todo|todo fiado|a credito|me lo debe|me los debe|me la debe|me las debe|no me pago|me queda debiendo|me quedo debiendo)\b", t)
            or (re.search(r"\bme debe\b", t) and not amts)) \
            and "paid_cents" not in out:
        out["fully_credit"] = True
    return out


def _sale_fields(t: str, products: list[ProductRef], p: Parsed) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pm = match_products(t, products)
    qty, qspan = find_quantity(t, [pm.span] if pm.span else [])
    if pm.candidates:
        out["product_candidates"] = [c.id for c in pm.candidates]
    elif qspan:
        name = guess_new_product(t, qspan)
        if name:
            out["new_product"] = name
    if qty is None and not pm.candidates:
        qm = re.search(r"\b(\d+) ([a-zñ]{3,})\b", t)
        if qm and qm.group(2) not in STOP and not re.match(MONEY_SUFFIX, qm.group(2)) and qm.group(2) not in MONEY_FOLLOWERS:
            qty, qspan = int(qm.group(1)), (qm.start(1), qm.end(1))
            name = guess_new_product(t, qspan)
            if name:
                out["new_product"] = name
    out["quantity"] = qty if qty is not None else (1 if (pm.candidates or out.get("new_product")) else None)
    if qty is None and pm.span:
        # «vendí un polo»: 'un' ya se convirtió a 1
        before = t[:pm.span[0]].split()
        if before and before[-1].isdigit():
            out["quantity"] = int(before[-1])
    m = find_method(t)
    if m:
        out["method"] = m
    amts = [a for a in find_amounts(t) if not (qspan and a.start <= qspan[0] < a.end)]
    roles = _payment_roles(t, amts)
    out.update({k: v for k, v in roles.items() if k != "total_cents"})
    price_amts = [a for a in amts if a.cents not in (roles.get("paid_cents"), roles.get("pending_cents"))]
    if roles.get("total_cents") is not None:
        out["total_cents"] = roles["total_cents"]
    elif price_amts:
        if len(price_amts) > 1:
            out["amount_options"] = [a.cents for a in price_amts]
        a = price_amts[0]
        pre = a.prefix[-20:]
        post = t[a.end:a.end + 20]
        if re.search(r"^\s*(cada uno|cada una|cada|por unidad|la unidad|c/u)", post) or re.search(r"\bde\s*$", pre):
            out["unit_price_cents"] = a.cents
        elif re.search(r"^\s*(en total|total|por todo|todo)", post) or re.search(r"(por|en total|total|por todo)\s*$", pre):
            out["total_cents"] = a.cents
        elif re.search(r"\ba\s*$", pre):
            out["ambiguous_price_cents"] = a.cents
        else:
            out["total_cents"] = a.cents
    elif roles.get("paid_cents") is not None and roles.get("pending_cents") is not None:
        out["total_cents"] = roles["paid_cents"] + roles["pending_cents"]
    person = find_person(t)
    if person:
        out["counterparty"] = person
    return out


def _purchase_fields(t: str, products: list[ProductRef], p: Parsed) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pm = match_products(t, products)
    qty, qspan = find_quantity(t, [pm.span] if pm.span else [])
    if pm.candidates:
        out["product_candidates"] = [c.id for c in pm.candidates]
    elif qspan:
        name = guess_new_product(t, qspan)
        if name and name not in ("mercaderia", "producto", "cosa"):
            out["new_product"] = name
    if qty is None and pm.span:
        before = t[:pm.span[0]].split()
        if before and before[-1].isdigit():
            qty = int(before[-1])
    mm = re.search(r"\b(\d+(?:\.\d+)?) (jabas?|sacos?|cajas?|kilos?|atados?|bolsas?|paquetes?) (?:de )?", t)
    if mm:   # «2 jabas de tomate»: la cantidad va en esa unidad
        out["measure"] = float(mm.group(1))
        out["measure_unit"] = MEASURE_UNITS.get(mm.group(2), mm.group(2).rstrip("s"))
        qty = None
    out["quantity"] = qty
    m = find_method(t)
    if m:
        out["method"] = m
    amts = [a for a in find_amounts(t) if not (qspan and a.start <= qspan[0] < a.end)]
    if amts:
        if len(amts) > 1:
            out["amount_options"] = [a.cents for a in amts]
        a = amts[0]
        pre = a.prefix[-20:]
        post = t[a.end:a.end + 20]
        if re.search(r"^\s*(cada uno|cada una|cada|por unidad|la unidad|el kilo|el litro)", post):
            out["unit_price_cents"] = a.cents
        elif re.search(r"^\s*(en total|total|por todo|todo)", post) or re.search(r"(por|en total|total|por todo|gaste|pague|en)\s*$", pre):
            out["total_cents"] = a.cents
        elif re.search(r"\ba\s*$", pre) and qty and qty > 1:
            out["ambiguous_price_cents"] = a.cents
        else:
            out["total_cents"] = a.cents
    if re.search(r"\b(mercaderia|insumos|productos)\b", t) and not pm.candidates:
        out["generic"] = True
    sm = re.search(r"\b(?:al|a la|a) (?:proveedor|proveedora|senor|senora|don|dona)?\s?([a-zñ]{3,})\b", t)
    if sm and sm.group(1) not in NAME_STOP and not any(sm.group(1) in product_terms(pr) for pr in products):
        out["counterparty"] = sm.group(1).capitalize()
    return out


def _catalog_fields(t: str, pm: ProductMatch) -> dict[str, Any]:
    out: dict[str, Any] = {"product_candidates": [c.id for c in pm.candidates]}
    if not pm.candidates:
        m = re.search(r"\bmi (?:producto )?([a-zñ]+(?: [a-zñ]+)?) (?:cuesta|lo vendo|vale|precio)", t)
        if m:
            out["new_product"] = singular(m.group(1).split()[0]) + ("" if len(m.group(1).split()) == 1 else " " + m.group(1).split()[1])
    amts = find_amounts(t)
    for a in amts:
        pre = a.prefix[-25:]
        if re.search(r"(lo vendo a|los vendo a|la vendo a|las vendo a|vendo a|precio de venta|precio|lo vendo en|vale|valen|cobro)\s*$", pre):
            out["price_cents"] = a.cents
        elif re.search(r"(me cuesta|me cuestan|lo compro a|los compro a|costo|lo compro en|pago)\s*$", pre):
            out["cost_cents"] = a.cents
        elif re.search(r"(cuesta|cuestan)\s*$", pre):
            out["ambiguous_cents"] = a.cents
    sm = re.search(r"\b(?:tengo|hay|quedan|me quedan)\s+(\d+)\b(?:\s+\w+)?\s*(?:en stock|en tienda|unidades|$)", t) \
        or re.search(r"\b(\d+)\s+(?:\w+\s+)?en stock\b", t)
    if sm:
        out["stock"] = int(sm.group(1))
    return out


def _goal_fields(t: str, today: date) -> dict[str, Any]:
    out: dict[str, Any] = {}
    amts = find_amounts(t) or _bare_amounts(t)
    amts = [a for a in amts if a.cents >= 1000]   # «una meta» / «en 3 meses» no son S/ 1 ni S/ 3: sin monto, se le pregunta
    if amts:
        out["target_cents"] = max(a.cents for a in amts)
    skip = {"enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "setiembre",
            "octubre", "noviembre", "diciembre", "navidad", "fin", "el", "la"}
    for nm in re.finditer(r"\b(comprar|abrir|para|reponer)\s+(?:(?:una|un|1|la|el|otro|otra|mi|mis|los|las)\s+)?([a-zñ ]+?)(?=\s+(?:de|en|para|antes|que|a)\b|\s+\d|$)", t):
        name = nm.group(2).strip()
        if name and name.split()[0] not in skip:
            verb = nm.group(1)
            out["name"] = (("Abrir " + name) if verb == "abrir" else name).capitalize() if verb != "abrir" else ("Abrir " + name)
            break
    if re.search(r"\b(reponer|mercaderia|reposicion|stock)\b", t):
        out["kind"] = "reposicion"
    else:
        out["kind"] = "ahorro"
    dl = None
    mm = re.search(r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\b", t)
    if mm:
        months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        name = "septiembre" if mm.group(1) == "setiembre" else mm.group(1)
        mi = months.index(name) + 1
        y = today.year if mi >= today.month else today.year + 1
        import calendar
        dl = date(y, mi, min(calendar.monthrange(y, mi)[1], 20 if name == "diciembre" else 28))
    if re.search(r"\b(navidad|navidena|campana navidena)\b", t):
        y = today.year if today.month <= 12 else today.year + 1
        dl = date(y, 12, 15)
    wm = re.search(r"\ben (\d+) (semanas|meses|dias)\b", t)
    if wm:
        n = int(wm.group(1))
        days = n * (7 if wm.group(2) == "semanas" else 30 if wm.group(2) == "meses" else 1)
        dl = today + timedelta(days=days)
    if dl:
        out["deadline"] = dl.isoformat()
    return out


def _balances(t: str) -> dict[str, int]:
    out: dict[str, int] = {}
    used: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:s/ ?)?(\d+(?:\.\d{1,2})?)(?: ?soles)?\s+(?:en|de|por)?\s*(?:el |la |mi )?(efectivo|yape|plin|caja|monedero|bolsillo|billetera|mano|sencillo|cajon)", t):
        pocket = "yape" if m.group(2) in ("yape", "plin") else "efectivo"
        out[pocket] = int(round(float(m.group(1)) * 100))
        used.append(m.span(1))
    for m in re.finditer(r"\b(efectivo|yape|caja)\s+(?:tengo\s+)?(?:s/ ?)?(\d+(?:\.\d{1,2})?)", t):
        pocket = "yape" if m.group(1) == "yape" else "efectivo"
        out.setdefault(pocket, int(round(float(m.group(2)) * 100)))
    if not out:
        amts = find_amounts(t) or _bare_amounts(t)
        if amts:
            out["efectivo"] = amts[0].cents
    elif "efectivo" not in out:   # «tengo 80 soles y 50 en yape»: el monto suelto es el efectivo
        for m in re.finditer(r"\b(\d+(?:\.\d{1,2})?)\b", t):
            if not any(a <= m.start() < b for a, b in used):
                out["efectivo"] = int(round(float(m.group(1)) * 100))
                break
    return out



# ---------------------------------------------------------------- varias ventas en un mensaje (mercado)

MEASURE_UNITS = {"kilos": "kg", "kilo": "kg", "caja": "caja", "cajas": "caja", "paquete": "paquete", "paquetes": "paquete", "atado": "atado", "atados": "atado", "jaba": "jaba", "jabas": "jaba",
                 "saco": "saco", "sacos": "saco", "manojo": "manojo", "manojos": "manojo", "bolsa": "bolsa", "bolsas": "bolsa",
                 "unidades": "unidad", "unidad": "unidad"}
LUMP_RE = r"\b(como|ultima hora|acumulado|en la manana|en la tarde|hasta ahora|en lo que va|ventas varias|de todo|en total vendi|vendi en total)\b"


def _num(x: str) -> float:
    return float(x)


def _mentions(t: str, products: list[ProductRef]) -> list[tuple[int, int, ProductRef | None, str]]:
    """Productos mencionados en orden, sin solaparse. También «N soles de X» con X desconocido."""
    found: list[tuple[int, int, ProductRef | None, str]] = []
    for p in products:
        best = None
        for term in product_terms(p):
            for m in re.finditer(rf"\b{re.escape(term)}\b", t):
                cand = (m.start(), m.end(), p, term)
                if best is None or (m.end() - m.start()) > (best[1] - best[0]):
                    best = cand
                found.append(cand)
    # nombres nuevos: «3 soles de rocoto»
    for m in re.finditer(r"\b\d+(?:\.\d+)? (?:soles|kilos?|atados?|jabas?|sacos?|manojos?) de (?:la |el )?([a-zñ]{3,})\b", t):
        if m.group(1) not in STOP and m.group(1) not in UNIT_WORDS:
            found.append((m.start(1), m.end(1), None, singular(m.group(1))))
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out: list[tuple[int, int, ProductRef | None, str]] = []
    for f in found:
        if out and f[0] < out[-1][1]:
            continue
        if f[2] is None and any(o[2] is not None and o[0] == f[0] for o in out):
            continue
        if f[2] is not None and any(o[2] is not None and o[2].id == f[2].id for o in out):
            continue   # el mismo producto dos veces: se toma la primera mención
        out.append(f)
    return out


def parse_sale_items(t: str, products: list[ProductRef]) -> dict[str, Any] | None:
    """«vendí 3 soles de tomate y un kilo de papa», «tomate, cebolla y limón, todo 12 soles», «en la última hora vendí 60»."""
    method = find_method(t)
    out: dict[str, Any] = {}
    if method in ("efectivo", "yape"):
        out["method"] = method
    elif method == "mixto":
        out["method"] = "mixto"
    ments = _mentions(t, products)
    gm = re.search(r"\b(?:todo|en total|total|por todo|todos)\s*(?:a |por |en |de )?(?:s/ ?)?(\d+(?:\.\d+)?)", t) \
        or re.search(r"(?:s/ ?)?(\d+(?:\.\d+)?)\s*(?:soles\s*)?(?:en total|por todo|todo)\b", t)
    if not ments:
        amts = find_amounts(t) or [a for a in _bare_amounts(t)]
        if amts and _has(t, LUMP_RE):
            out.update({"lump": True, "group_total_cents": max(a.cents for a in amts), "items": []})
            return out
        return None
    if gm and len(ments) >= 2:
        out.update({"group_total_cents": int(round(_num(gm.group(1)) * 100)),
                    "items": [{"product_id": p.id if p else None, "name": p.name if p else term.capitalize(), "new": p is None}
                              for (_s, _e, p, term) in ments]})
        return out
    items = []
    prev_end = 0
    any_detail = False
    for idx, (s0, e0, p, term) in enumerate(ments):
        before = t[prev_end:s0]
        nxt = ments[idx + 1][0] if idx + 1 < len(ments) else len(t)
        after = t[e0:nxt]
        it: dict[str, Any] = {"product_id": p.id if p else None, "name": p.name if p else term.capitalize(), "new": p is None,
                              "amount_cents": None, "measure": None, "unit": None}
        m = re.search(r"(?:s/ ?)?(\d+(?:\.\d+)?) soles de\s*(?:la |el |los |las )?$", before)
        if m:
            it["amount_cents"] = int(round(_num(m.group(1)) * 100))
            any_detail = True
        m2 = re.search(r"(\d+(?:\.\d+)?) (kilos?|atados?|jabas?|sacos?|manojos?|bolsas?|unidades|unidad) (?:de\s*)?(?:la |el |los |las )?$", before)
        if m2:
            it["measure"] = _num(m2.group(1))
            it["unit"] = MEASURE_UNITS[m2.group(2)]
            any_detail = True
        elif not m:
            m3 = re.search(r"\b(\d+(?:\.\d+)?)\s*$", before)
            if m3 and not re.search(r"soles\s*$", before):
                it["measure"] = _num(m3.group(1))
                it["unit"] = "unidad"
        ma = re.match(r"\s*(?:a|por|en)\s+(?:s/ ?)?(\d+(?:\.\d+)?)(?:\s*soles)?(\s*(?:el kilo|cada|c/u|cada uno|la unidad|el atado))?", after)
        if ma:
            val = int(round(_num(ma.group(1)) * 100))
            if ma.group(2) and it["measure"]:
                it["amount_cents"] = round(val * it["measure"])
            else:
                it["amount_cents"] = val
            any_detail = True
        items.append(it)
        prev_end = e0
    if len(items) >= 2 or any_detail and any((i["measure"] and i["unit"] != "unidad") or i["amount_cents"] for i in items) \
            and not all(i["unit"] == "unidad" and i["amount_cents"] is None for i in items):
        # una sola venta «normal» por unidades (ej. polos con descuento) sigue el camino clásico
        if len(items) == 1 and items[0]["unit"] in (None, "unidad") and not re.search(r"soles de", t):
            return None
        out["items"] = items
        return out
    return None



# ---------------------------------------------------------------- pérdidas

LOSS_RE = (r"\b(se (?:me |nos |le )?(?:malogr|pudri|podri|venci|vencio|rompi|rompio|quebr|cay|derram|perdi|quem|manch|moj|dan|arruin|"
           r"marchit|apolill|chanc|reventa|reventaron|reventó|desperdici)\w*|me robaron|nos robaron|me han robado|me rob[oó]|robo\b|"
           r"billete falso|plata falsa|perdi\b|perdi (?:plata|soles|\d)|regale|merma|perdida|se echo a perder|se echaron a perder|"
           r"ya no sirve|tuve que botar|bote\b|botamos)")
LOSS_REASONS = [(r"rob", "robo"), (r"falso|falsa", "billete falso"), (r"venci", "vencido"), (r"romp|quebr|cay|revent|chanc", "roto"),
                (r"regal", "regalo"), (r"quem", "quemado"), (r"malogr|pudri|podri|marchit|echo a perder|echaron a perder|bot", "malogrado"),
                (r"moj|manch|dan|arruin", "dañado"), (r"perdi", "perdido")]


def loss_fields(t: str, products: list[ProductRef]) -> dict[str, Any]:
    """«se me malogró medio saco de tomate», «me robaron 50 soles», «se me rompió la balanza, valía 120»."""
    out: dict[str, Any] = {"kind": "perdida"}
    reason = next((r for pat, r in LOSS_REASONS if re.search(pat, t)), "pérdida")
    out["reason"] = reason
    pm = match_products(t, products)
    if pm.candidates:
        out["product_candidates"] = [c.id for c in pm.candidates]
    mm = re.search(r"\b(\d+(?:\.\d+)?) (kilos?|jabas?|sacos?|cajas?|atados?|bolsas?|paquetes?|unidades|docenas?|bandejas?)\b", t)
    if mm:
        out["measure"] = float(mm.group(1))
        out["measure_unit"] = MEASURE_UNITS.get(mm.group(2), mm.group(2).rstrip("s"))
    else:
        qty, _ = find_quantity(t, [pm.span] if pm.span else [])
        if qty:
            out["measure"] = float(qty)
            out["measure_unit"] = "unidad"
    money = [int(round(float(x) * 100)) for x in re.findall(r"(?:valia|vale|costo|costaba|como|unos|eran|era|de|por|en)\s+(?:s/ ?)?(\d+(?:\.\d{1,2})?)(?!\s*(?:kilos?|jabas?|sacos?|cajas?|atados?|unidades|docenas?|bandejas?|\.\d))", t)] \
        + [int(round(float(x) * 100)) for x in re.findall(r"(?:s/ ?)(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?) soles", t) for x in x if x]
    if mm:
        money = [m for m in money if m != int(round(float(mm.group(1)) * 100))]
    if money:
        out["amount_cents"] = money[0]
    method = find_method(t)
    is_money = reason in ("robo", "billete falso") or bool(re.search(r"\b(plata|soles|sencillo|billete|monedero|caja)\b", t) and not pm.candidates and not mm
                                                       and reason in ("perdido", "pérdida", "robo", "billete falso"))
    if is_money and not pm.candidates:
        out["loss_kind"] = "plata"
        if method in ("efectivo", "yape"):
            out["method"] = method
        elif reason == "billete falso" or re.search(r"\b(billete|monedero|sencillo|caja)\b", t):
            out["method"] = "efectivo"
    elif pm.candidates or re.search(r"\b(mercaderia|producto|productos|verdura|fruta|mercancia)\b", t):
        out["loss_kind"] = "mercaderia"
    else:
        out["loss_kind"] = "otro"
    # qué se perdió (para «otro» o productos que no están en la lista)
    dm = re.search(r"(?:se (?:me |nos |le )?\w+|me robaron|regale|tuve que botar|bote)\s+(?:el |la |los |las |un |una |1 |mi |mis )?(.+?)(?:\s+(?:valia|que valia|eran|era|de \d|por \d|como \d|a \d|y me costo|costaba|me costo|y valia)\b|$)", t)
    if dm and dm.group(1).strip() and not re.match(r"^\d", dm.group(1).strip()):
        out["what"] = re.sub(r"\s*(?:s/ ?)?\d+(?:\.\d+)?(?: soles?)?$", "", dm.group(1).strip()) or None
    return out
