# Kallpa — asistente financiero por voz para microemprendedoras

MVP para la hackathon BCP. La emprendedora **dicta** lo que pasó en su negocio («vendí tres polos por cincuenta soles en efectivo»), el asistente lo entiende, pregunta solo lo que falta (una pregunta por turno), pide confirmación y lo registra. Desde la app ve su caja, su mercadería, sus metas y descarga un **reporte de ingresos y capacidad de pago** para presentar a BCP o a una caja.

- **Backend**: Python 3.12 + FastAPI + Pydantic + SQLAlchemy 2 + SQLite (importes en céntimos enteros). Reglas propias en Python: interpretan el dictado, calculan descuentos, caja, inventario, metas, conciliación y reporte. **Sin IA para las operaciones diarias.**
- **App**: React + TypeScript + Vite como **PWA** instalable (service worker + IndexedDB para audios pendientes sin señal). Se puede empaquetar como **APK Android** con Capacitor.
- **Voz**: MediaRecorder en la app → **faster-whisper** (`base`, CPU, int8) en el servidor. Respaldo: reconocimiento de voz del navegador. Las respuestas se leen en voz alta.
- **Gemini**: una sola solicitud por negocio y solo con la descripción del rubro, para proponer la estructura inicial. Si no hay clave o falla → plantillas locales (reventa, comida preparada, servicios).
- **WhatsApp Cloud API** (Meta): mismo motor de conversación. Si no hay credenciales, el chat web es el canal y WhatsApp aparece «pendiente de conectar».

## Requisitos
- Python 3.12+ y Node.js 18+ (probado con Node 22)

## 1. Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env            # opcional: ninguna clave es obligatoria
python scripts/download_model.py   # descarga el modelo de voz (una vez, ~140 MB) antes de la demo
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Al iniciar carga **datos de demostración** (dos negocios ficticios: «Polos Rosita» – reventa y «Cocina de Doña Carmen» – comida). Para empezar en blanco usa `SEED_DEMO=false`.

Pruebas (casos de aceptación):
```bash
cd backend && python -m pytest -q
```

## 2. App (desarrollo)
```bash
cd frontend
npm install
npm run dev        # http://localhost:5173  (redirige /api al backend :8000)
```

## 3. Demo en un solo servidor (recomendado para presentar)
```bash
cd frontend && npm install && npm run build
cd ../backend && uvicorn app.main:app --host 0.0.0.0 --port 8000
```
El backend sirve la app compilada en `http://localhost:8000`. En la computadora, `localhost` permite usar el micrófono.

**En el celular**, el micrófono y la instalación de la PWA requieren HTTPS. Expón el puerto con un túnel, por ejemplo:
```bash
cloudflared tunnel --url http://localhost:8000
```
Abre la URL `https://…` en Chrome del celular → menú ⋮ → **Agregar a pantalla de inicio**. Queda como app; tras la primera carga abre sin señal y guarda los audios como «Pendiente de enviar».

## 4. APK Android (Capacitor)
Requiere Android Studio.
```bash
cd frontend
npm install
npm run android:init     # build + agrega plataforma android + permisos de micrófono
npm run android:open     # abre Android Studio → Run / Build APK
```
En la app: **Más → Ajustes → Servidor** coloca la URL del backend (por ejemplo la del túnel HTTPS o `http://IP-de-tu-PC:8000` en la misma red Wi-Fi). Tras cambios: `npm run android:sync`.

## Recorrido de demo (5 min)
1. **Inicio** → «Empezar mi negocio» → «Cuéntame qué vendes y cómo te pagan» por voz → revisa la propuesta → Sí, guardar. (O usa una plantilla.)
2. O entra a **Polos Rosita** (demo) y ve el panel: ¿Cuánto cobré? ¿Cuánto tengo? ¿Cuánto puedo usar para reponer? Flujo de caja 30 días, margen por producto, punto de equilibrio, rotación, mejores días, consejos.
3. **Dictar** (micrófono): «Vendí tres polos por cincuenta soles en efectivo» → «Tres polos básicos, S/ 50 en efectivo, con S/ 10 de descuento. ¿Lo guardo?» → «sí».
4. «Compré una docena de polos por 144 en total y pagué por Yape» → 12 unidades, S/ 12 c/u.
5. Activa modo avión, graba un audio → queda **Pendiente de enviar** → vuelve la señal → se procesa una sola vez.
6. «Conté 17 polos» → diferencia y qué revisar (Mercadería → Ajustar o Dejar pendiente).
7. «¿Qué es eso del flujo de caja?» → explicación con sus propios números.
8. **Metas**: «quiero juntar 1500 para una congeladora en diciembre».
9. **Más → Reporte para el banco** → pregunta mensual de envío al BCP (simulado) → Descargar PDF.

## Primera vez: el inventario se arma conversando
Al abrir la app se elige el canal (WhatsApp o el chat de la app; es el mismo asistente). El asistente pregunta, una cosa a la vez y sin hablar de «inventario»: nombre → qué vende → ¿algo más? → cómo lo vende (kilo, unidad, atado, de a sol) → a cuánto lo vende y cuánto le cuesta → cómo compra en el mayorista (jaba de 20 kg, saco de 50 kg, docena…) → cuánto le queda → cómo le pagan → cuánta plata tiene. Muestra un resumen, acepta correcciones («el tomate es a 3.50», «agrega rocoto a 8», «quita el ajo») y recién al confirmar empieza a recibir ventas y gastos.

Cada producto queda como una **ficha JSON flexible** (`Product.spec`), por ejemplo:
```json
{"base": "kg", "vende_por": [{"unidad": "kg", "precio_cents": 400}],
 "compra_por": {"unidad": "jaba", "equivale": 20, "costo_cents": 5000},
 "stock_inicial": {"cantidad": 2, "unidad": "jaba", "en_base": 40}}
```
Con esa ficha la app convierte unidades: «una jaba de tomate» se cobra 20 kg × precio del kilo, «3 soles de tomate» descuenta 0.75 kg del stock, «compré 2 jabas a 100» suma 40 kg.

## Agente de IA (Women2) en la app y en WhatsApp
Se integró el agente de [Women2](https://github.com/maydelithzuniga/Women2) (`backend/app/agent.py`, `backend/app/inbox.py`):
- **Un solo cerebro para los dos canales**: el chat de la app y WhatsApp aceptan **texto o audio** y pasan por la misma puerta (`inbox.receive`).
- **Gemini multimodal** escucha el audio (o lee el texto) con el contexto del negocio y devuelve el JSON de intención del prompt de Women2, ampliado: venta, compra, gasto, retiro, pérdida, cobro de fiado, meta, cierre y consulta.
- La intención **no se guarda directo**: entra al motor de Kallpa, que pregunta lo que falta, evita duplicados (id de mensaje) y actualiza caja e inventario. Por eso la app y WhatsApp muestran siempre lo mismo.
- Las respuestas a preguntas abiertas («efectivo», «30») se resuelven con reglas, sin gastar IA. Las consultas («¿cómo voy?») las responde el agente con sus números.
- **Respuesta en voz por WhatsApp** con Edge-TTS (es-PE-CamilaNeural), servida en `/audio/{archivo}` (necesita `APP_BASE_URL` público).
- **Plantillas de WhatsApp** (`WHATSAPP_TEMPLATE_RESUMEN`) para los mensajes automáticos fuera de la ventana de 24 h.
- Si no hay `GEMINI_API_KEY` o el agente falla, todo sigue funcionando con las reglas locales y Whisper.
- Para activarlo: `AGENT_ENABLED=true`, `GEMINI_API_KEY=…`, `AGENT_MODEL=…` en `backend/.env`. Nota: con el agente activo, cada mensaje se envía a Gemini junto con un resumen del negocio.

## Mensajes automáticos (máximo 2 al día)
- **Mañana**: avisos agrupados que piden acción: pago cercano («Mañana toca el alquiler, S/ 250»), stock bajo («Te quedan 3 kg de tomate, te alcanza para hoy»), costo que subió en el mayorista, fiado con más de 15 días, meta atrasada. El **día 1** va con el resumen del mes («En octubre ganaste S/ 2,300, 12% más… Tu informe para el BCP está listo: ¿lo envío? Podrías pagar una cuota de hasta S/ 250»).
- **Noche**: cómo te fue hoy («Hoy vendiste S/ 380 y ganaste S/ 95, más que tu promedio… Mañana compra más papa»). Si no registró nada, un recordatorio suave. El **domingo**, qué aprendiste de tu semana.
- No se repite la misma alerta en 3 días. Se envían por WhatsApp si está conectado y siempre quedan en el chat. Para producción, llama cada hora a `POST /api/notifications/run-all` (cron). En la app: Ajustes → «Mensajes automáticos» para probarlos.
- **Lista de cobros para la vendedora**: el resumen de la noche, el del domingo y el del día 1 terminan con «Por cobrar (S/ 42): Doña Rosa S/ 30, hace 18 días; Vecina Carmen S/ 12, hace 4 días». También responde a «¿quién me debe?». Nunca se le escribe a la casera.
- **Contar la plata** en vez de «saldo inicial»: «quiero contar mi plata» → «Cuenta la plata que tienes ahora en tu monedero».

## Pérdidas (cualquier rubro)
Se declaran por chat: «se me malogró medio saco de tomate», «se mancharon 2 polos», «se me venció el yogurt, eran 30 soles», «me robaron 50 soles», «me pagaron con un billete falso de 20», «se me rompió la balanza». El asistente pregunta solo lo que falta (qué fue, cuánto valía, si era efectivo o Yape) y lo guarda como **pérdida**: baja la ganancia, descuenta stock si corresponde y, si fue plata, la caja. En el cierre de la noche pregunta «¿Tuviste alguna pérdida hoy?».

## Cómo se usa en el día (verdulería u otro negocio)
- **Entre cliente y cliente**, un audio corto: «vendí 3 soles de tomate y un kilo de papa en efectivo». Se guarda al toque y responde «✓ S/ 3 tomate, S/ 2.50 papa (1 kg) · efectivo». Si se equivocó: «deshacer» o «corrige: fue por Yape».
- **Varias cosas con un solo precio**: «vendí tomate, cebolla y limón, todo 12 soles».
- **Si no pudo anotar cada venta**: «en esta última hora vendí como 60 en efectivo».
- **Solo pregunta lo que falta** (una pregunta): el medio de pago o el precio de algo que no está en su catálogo.
- **Cierre de la noche** («cerrar mi día»): cuenta su monedero, la app compara con lo anotado; si sobra plata la guarda como ventas no anotadas, si falta pregunta si fue para la casa o un gasto; y pregunta por la merma («media jaba de tomate, 25 soles»).
- **Yape**: todo se registra por audio. El registro de Yape solo sirve para **verificar** lo que dijo «por Yape» (mismo monto, día y sentido); nunca crea ventas, así no se duplica. Solo lo verificado entra al informe del BCP.

## Frases que entiende (ejemplos)
| Dicta | Registra |
|---|---|
| vendí 3 polos a 25 por Yape | venta (pregunta total/unidad si es ambiguo) |
| vendí 2 polos a la señora Juana, me pagó 30 y me debe 10 | venta con fiado parcial |
| Juana me pagó 10 del fiado | cobro vinculado a la venta |
| compré mercadería por 144 | compra con unidades pendientes |
| pagué la luz 80 / gasté 15 en pasajes | gasto del negocio (fijo / variable) |
| me saqué 100 para la casa | retiro personal (no es gasto del negocio) |
| puse 200 de mi bolsillo | aporte (no es venta) |
| pasé 100 de Yape a efectivo | traslado (no cambia ventas) |
| pagué la cuota del préstamo | pago de deuda |
| mi polo cuesta 20 y tengo 10 en stock | catálogo (pregunta si 20 es precio o costo) |
| conté 17 polos / tengo 300 en efectivo | conteo y conciliación |
| empiezo con 200 en efectivo y 150 en Yape | saldo inicial |
| corrige: eran dos polos / anula la última | corrección con historial / anulación |
| resumen · ¿cuánto tengo? · consejos · fiados · ¿qué es el margen? | respuestas con sus datos |

## Coach de metas y productos financieros
Cuando la emprendedora dice «quiero ponerme una meta», Kallpa le **pregunta de cuánto y para qué**, le propone metas con sus números y, al guardar, le arma el **plan para llegar** (cuánto separar al día, qué tan llevadero es con lo que gana, cómo llegar antes) y le recomienda **productos financieros** según el caso: ahorro siempre, crédito solo si la meta es para el negocio y la cuota cómoda le alcanza. Detalles en [`docs/COACH_FINANCIERO.md`](docs/COACH_FINANCIERO.md).

| Dice | Kallpa responde |
|---|---|
| quiero ponerme una meta | pregunta de cuánto y para qué, y propone 3 con sus números |
| 800 para una refrigeradora en 3 meses | guarda la meta con plan y productos financieros |
| dame consejos para llegar a mi meta | plan de su meta activa, con sus fiados, márgenes y pérdidas |
| ¿me conviene un préstamo? / necesito un préstamo de 3000 para surtir mi puesto | cuota estimada vs su cuota cómoda, dónde pedirlo y cuidados |

## Seguridad e idempotencia
- Cada mensaje tiene `message_id` y cada borrador un `op_id` único: reintentos de audio, webhooks repetidos de Meta o doble «sí» no duplican efectos.
- Caja e inventario se escriben en la misma transacción (líneas `cash_entries` / `stock_entries`). Las correcciones guardan la versión anterior en `movement_revisions`.
- Webhook de WhatsApp: verificación `hub.verify_token` y firma `X-Hub-Signature-256` con `WHATSAPP_APP_SECRET`. Credenciales solo en variables de entorno del servidor.

## ⚡ WhatsApp real y gratis para la demo: el puente
[`docs/WHATSAPP_GRATIS_PUENTE.md`](docs/WHATSAPP_GRATIS_PUENTE.md) — `WHATSAPP_PROVIDER=bridge`, carpeta `bridge/` (QR, chip aparte).

## WhatsApp con Twilio (requiere saldo)
[`docs/WHATSAPP_TWILIO.md`](docs/WHATSAPP_TWILIO.md) — `WHATSAPP_PROVIDER=twilio`, webhook `/webhook/twilio`.

## Guía completa (de cero a WhatsApp funcionando)
[`docs/GUIA_COMPLETA.md`](docs/GUIA_COMPLETA.md): instalar, publicar en Render (`render.yaml`), crear el número en Meta,
webhook, vincular el WhatsApp de la emprendedora, avisos automáticos (cron) y checklist para la presentación.

## WhatsApp real
Guía paso a paso: [`docs/CONECTAR_WHATSAPP.md`](docs/CONECTAR_WHATSAPP.md).

## WhatsApp (resumen)
1. Crea una app en Meta for Developers con WhatsApp Cloud API y un número de prueba.
2. Completa en `.env`: `WHATSAPP_ENABLED=true`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`.
3. Webhook: `https://TU-URL/webhook/whatsapp` (suscribe `messages`). Mensajes de texto y audios usan el mismo flujo; el resumen diario se envía con `POST /api/businesses/{id}/whatsapp/daily-summary`.

## Fuera del MVP
Scoring crediticio, aprobación de préstamos y conexiones bancarias reales. El envío al BCP es **simulado** (se registra la autorización mensual). Los datos de demostración son ficticios y se marcan como tales en el reporte.

## Estructura
```
backend/app/  nlu.py (dictado→borrador) · conversation.py (aclaración/confirmación) · ledger.py (caja+stock)
              metrics.py (indicadores) · reconcile.py (cruces) · report.py (reporte banco) · setup.py/gemini.py/templates.py
              transcribe.py (faster-whisper) · whatsapp.py · seed.py (demo) · main.py (API)
backend/tests/ casos de aceptación (pytest)
frontend/src/  pages: Onboarding, Chat, Home, Stock, Goals, Report, More · lib: api, outbox (IndexedDB), voice
```
