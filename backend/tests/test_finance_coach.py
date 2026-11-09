"""Coach financiero: pregunta de cuánto/para qué, plan para llegar, consejos con su meta y productos financieros.

Prueba también que lo que ya existía (registro de ventas/pagos, propuestas 1-2-3) sigue igual.
"""
from datetime import date

from app import finance_coach as fc
from app import nlu
from app.db import SessionLocal
from app import models as M


def goals(client, bid):
    return [g for g in client.get(f"/api/businesses/{bid}/dashboard").json()["goals"] if g["status"] == "activa"]


# --- «quiero ponerme una meta» ------------------------------------------------------------------
def test_meta_sin_monto_pregunta_de_cuanto_y_para_que_y_no_guarda_meta_de_1_sol(client, shop, chat):
    r = chat("quiero ponerme una meta")
    assert "de cuánto la quieres y para qué" in r["reply"]
    assert "1️⃣" in r["reply"]                       # las propuestas con sus números siguen ahí
    assert goals(client, shop["id"]) == []           # antes guardaba «Mi meta» de S/ 1 sin preguntar


def test_meta_con_monto_sin_nombre_pregunta_para_que_es_y_guarda_con_plan(client, shop, chat):
    chat("quiero ponerme una meta")
    r = chat("1500")
    assert "¿Para qué es?" in r["reply"] and "S/ 1,500" in r["reply"]
    r = chat("una congeladora")                       # «una» no debe cambiar el monto a S/ 1
    assert "Meta guardada" in r["reply"] and "S/ 1,500" in r["reply"]
    assert "Tu plan para «Congeladora»" in r["reply"]
    assert "Productos financieros" in r["reply"] and "TCEA" in r["reply"]
    g = goals(client, shop["id"])
    assert len(g) == 1 and g[0]["target_cents"] == 150000 and g[0]["name"] == "Congeladora"


def test_su_propia_meta_con_plazo_no_se_confunde_con_la_opcion_3(client, shop, chat):
    chat("quiero ponerme una meta")
    r = chat("800 para una refrigeradora en 3 meses")   # «3 meses» no es «la 3»
    assert "Meta guardada" in r["reply"] and "Refrigeradora" in r["reply"]
    assert "Separa" in r["reply"] and "al día" in r["reply"]
    g = goals(client, shop["id"])
    assert len(g) == 1 and g[0]["target_cents"] == 80000 and g[0]["deadline"]


def test_propuestas_1_2_3_siguen_funcionando_y_ahora_traen_plan(client, shop, chat):
    assert "1️⃣" in chat("metas")["reply"]
    r = chat("la 1")["reply"]
    assert r.startswith("✓ ¡Meta guardada!")
    assert "Tu plan para" in r
    assert len(goals(client, shop["id"])) == 1


# --- consejos y préstamos -----------------------------------------------------------------------
def test_consejos_sin_meta_invita_a_ponerse_una(client, shop, chat):
    r = chat("consejos")["reply"]
    assert "¿Quieres llegar a algo concreto?" in r


def test_consejos_para_llegar_a_mi_meta_da_el_plan(client, shop, chat):
    chat("quiero juntar 1500 para una congeladora en 10 semanas")
    r = chat("dame consejos para llegar a mi meta")["reply"]
    assert "Tu plan para «Congeladora»" in r and "Para llegar más rápido" in r
    assert "Productos financieros" in r
    assert "consejos" in chat("mis metas")["reply"]   # la pista para pedir consejos


def test_pregunta_de_prestamo_sin_monto_pregunta_de_cuanto_y_para_que(client, shop, chat):
    r = chat("¿me conviene un préstamo?")["reply"]
    assert "¿de cuánto sería y para qué lo usarías?" in r
    assert "gota a gota" in r and "TCEA" in r and "SBS" in r


def test_prestamo_con_monto_y_para_que(client, shop, chat):
    r = chat("necesito un préstamo de 3000 para surtir mi puesto")["reply"]
    assert "S/ 3,000" in r and "capital de trabajo" in r and "Mibanco" in r


def test_consejos_financieros_generales(client, shop, chat):
    r = chat("dame consejos financieros")["reply"]
    assert "Productos financieros" in r and "Dime de cuánto y para qué" in r


# --- lo que NO debe cambiar ---------------------------------------------------------------------
def test_pagar_una_cuota_sigue_siendo_un_registro_no_un_consejo(client, shop, chat):
    r = chat("pagué la cuota del préstamo 200 en efectivo")["reply"]
    assert "Productos financieros" not in r and "gota a gota" not in r


def test_venta_a_credito_sigue_siendo_una_venta(client, shop, chat):
    r = chat("le vendí 2 polos por 40 a crédito a Rosa")["reply"]
    assert "Productos financieros" not in r


def test_meta_con_monto_y_nombre_se_guarda_igual_que_antes(client, shop, chat):
    r = chat("quiero juntar 1500 para una congeladora en 10 semanas")["reply"]
    assert "Meta guardada" in r
    assert "vas al 2%" in chat("separé 30 para la congeladora en efectivo")["reply"]


# --- unidades -----------------------------------------------------------------------------------
def test_proposito_de_la_meta():
    assert fc.purpose("Colchón para emergencias") == "seguridad"
    assert fc.purpose("Mis gastos fijos del mes") == "seguridad"
    assert fc.purpose("Reponer mi mercadería") == "mercaderia"
    assert fc.purpose("Mi meta", "reposicion") == "mercaderia"
    assert fc.purpose("Refrigeradora") == "equipo"
    assert fc.purpose("Un viaje") == "otro"


def test_cuota_y_prestamo_maximo_son_coherentes():
    cuota = fc.installment(300000, 12)
    assert 29000 < cuota < 30500                       # S/ 3,000 a 12 meses con 40% de TEA ≈ S/ 298
    assert abs(fc.max_loan(cuota, 12) - 300000) <= 10000


def test_meta_propia_distingue_opciones_de_montos():
    hoy = date(2026, 11, 9)
    assert fc.custom_goal_fields("la 3", hoy) is None
    assert fc.custom_goal_fields("3", hoy) is None
    assert fc.custom_goal_fields("la 1 pero con 200", hoy) is None
    assert fc.custom_goal_fields("opción 2", hoy) is None
    f = fc.custom_goal_fields("800", hoy)
    assert f["target_cents"] == 80000 and f["ask_purpose"] is True
    f = fc.custom_goal_fields("1500 para una refrigeradora en 3 meses", hoy)
    assert f["target_cents"] == 150000 and f["name"] == "Refrigeradora" and f["ask_purpose"] is False


def test_una_meta_no_tiene_monto_de_1_sol():
    p = nlu.parse("quiero ponerme una meta", [], date(2026, 11, 9))
    assert p.intent == "meta" and not p.fields.get("target_cents")


def test_colchon_y_gastos_fijos_no_recomiendan_prestamo(client, shop):
    with SessionLocal() as db:
        b = db.get(M.Business, shop["id"])
        txt = fc.financing_text(db, b, name="Colchón para emergencias", remaining=30000, level="pesado")
        assert "no te conviene" in txt and "Crédito:" not in txt
        txt = fc.financing_text(db, b, name="Refrigeradora", remaining=150000, level="pesado")
        assert "Crédito:" in txt and "gota a gota" in txt
