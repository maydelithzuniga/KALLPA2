# Coach de metas y productos financieros

Módulo: `backend/app/finance_coach.py`. Solo **responde**: no guarda movimientos ni cambia datos por su cuenta.
Todo es Python puro (sin librerías extra), así que también corre en la demo del navegador (Pyodide).

## Qué hace

1. **Pregunta lo que falta.** «quiero ponerme una meta» → «¿de cuánto la quieres y para qué?». Si dice el monto pero no el motivo, pregunta «¿Para qué es?».
2. **Arma el plan para llegar** (al guardar una meta, y con «consejos para llegar a mi meta»):
   - cuánto separar al día y a la semana, y para qué fecha;
   - qué porcentaje de su ganancia de los últimos 30 días representa (llevadero / exigente / pesado);
   - cómo llegar antes, con **sus** números: fiados por cobrar, y los consejos de `metrics.advice` (comprar por jaba, margen por producto, mermas…).
3. **Recomienda productos financieros según el caso:**

   | Meta | Recomendación |
   |---|---|
   | Colchón de emergencia, gastos fijos | Solo ahorro. **Nunca** un préstamo. |
   | Mercadería, equipo, ampliar el negocio | Ahorro + crédito de capital de trabajo / activo fijo / MYPE, con la cuota estimada contra su **cuota cómoda** (`report.capacity`, la misma del informe para el banco). |
   | Otra cosa (personal) | Ahorro; crédito solo si con ahorro le resulta muy pesado. |

4. **Responde preguntas de préstamos:** «¿me conviene un préstamo?», «necesito un préstamo de 3000 para surtir mi puesto», «¿cuánto puedo pedir?», «dame consejos financieros».

## Cuidados que incluye en cada recomendación de crédito
- Cuota **referencial** (TEA de 40%, la misma del informe BCP); se dice que cada entidad aplica la suya.
- Advierte contra el «gota a gota» y los prestamistas informales.
- Sugiere entidades supervisadas por la SBS y comparar la **TCEA** (costo total), no solo la tasa.
- Si aún no hay meses suficientes de datos, lo dice en vez de inventar una cuota.
- Cierra con «es una orientación general: confirma las condiciones con la entidad».

## Dónde se conecta (cambios mínimos)
| Archivo | Cambio |
|---|---|
| `app/finance_coach.py` | **Nuevo.** Toda la lógica. |
| `app/goals_coach.py` | `intro/start(ask_amount=True)`; acepta su propia meta («800 para una refri en 3 meses»); agrega el plan al elegir una propuesta. |
| `app/conversation.py` | `fresh`: pregunta de cuánto/para qué; `step_goal`: «¿Para qué es?»; `confirm_goal`: agrega el plan; `answer_question`: `consejos` con su meta y la intención `consejos_financieros`. |
| `app/nlu.py` | Intención `consejos_financieros` (préstamos / consejos financieros) y una meta menor a S/ 10 cuenta como «sin monto». |
| `app/agent.py` | El prompt de Gemini pregunta el monto, aconseja y recomienda productos; el contexto incluye `ganancia_ultimos_30_dias` y `capacidad_credito`. |
| `tests/test_finance_coach.py` | **Nuevo.** 17 tests (comportamiento nuevo y lo que no debe cambiar). |

Todos los puntos de conexión están en `try/except`: si algo del coach falla, la respuesta vuelve a ser la de siempre.

## Dos errores que ya existían y quedaron corregidos
- «quiero ponerme una meta» guardaba **al toque** una meta de **S/ 1** llamada «Mi meta» (el «una» se leía como el número 1). Por eso nunca preguntaba de cuánto.
- Al contestar «una congeladora» a una pregunta sobre la meta, el «una» pisaba el monto con S/ 1.

## Ajustar los textos
Las entidades, la TEA de referencia y los porcentajes (30% llevadero, 60% exigente) están al inicio de `finance_coach.py` y en `financing_text` / `goal_plan`.
Si cambias de país o quieres otras entidades, edita solo `financing_text`.
