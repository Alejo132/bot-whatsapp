import os
import json
import sqlite3
from datetime import datetime, time
import pytz
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, request, Response, render_template_string
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

load_dotenv()

app = Flask(__name__)

# Inicializar DB al arrancar (funciona con gunicorn también)
def init_db():
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL,
            rol TEXT NOT NULL,
            contenido TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            hora INTEGER DEFAULT NULL
        )
    """)
    # Agregar columna hora si no existe (migracion para bases de datos existentes)
    try:
        c.execute("ALTER TABLE mensajes ADD COLUMN hora INTEGER DEFAULT NULL")
    except Exception:
        pass  # columna ya existe
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL,
            primer_contacto DATETIME DEFAULT CURRENT_TIMESTAMP,
            ultimo_contacto DATETIME DEFAULT CURRENT_TIMESTAMP,
            total_mensajes INTEGER DEFAULT 0,
            reservas INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
twilio = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

ZONA_HORARIA = pytz.timezone("America/Montevideo")

# ── Configuración del negocio ──────────────────────────────────────────────────
NEGOCIO = {
    "nombre": "Beauty Planet",
    "tipo": "spa y centro estético",
    "locales": {
        "Punta Carretas": "Punta Carretas Shopping, Nivel 1, José Ellauri 350, tel: 2711 9115",
        "Tres Cruces": "Shopping Tres Cruces, Julio Herrera y Obes 1234",
        "Centro": "Blanca del Tabaré 2990",
    },
    "horario": "lunes a viernes de 9:00 a 20:00, sábados de 10:00 a 18:00",
    "horario_apertura": time(9, 0),
    "horario_cierre": time(20, 0),
    "servicios": """
    💆 MASAJES Y RELAX
    - Masaje relajante (50 min)
    - Masaje descontracturante (50 min)
    - Masaje de piedras calientes
    - Day Spa & Relax (circuito completo)

    ✨ DEPILACIÓN DEFINITIVA
    - Luz pulsada y láser
    - Zonas pequeñas (axilas, bozo, mentón, etc.): $790
    - Consultar precio por zona mayor

    🧖 ESTÉTICA FACIAL Y CORPORAL
    - Limpieza de cutis
    - Tratamientos faciales
    - Hidrojet
    - Sol Free (ducha solar)
    - Cama solar

    🧘 BIENESTAR
    - Gimnasia y yoga
    - Sauna
    """,
    "reservas": "Se pueden hacer reservas para todos los servicios llamando o por WhatsApp.",
    "numero_dueno": os.getenv("NUMERO_DUENO", ""),
}

SYSTEM_PROMPT = f"""Sos el asistente virtual de {NEGOCIO['nombre']}, un {NEGOCIO['tipo']} en Montevideo, Uruguay.

Tu trabajo es atender a los clientes por WhatsApp de forma amigable, cálida y profesional.

INFORMACIÓN DEL NEGOCIO:
- Nombre: {NEGOCIO['nombre']}
- Horario: {NEGOCIO['horario']}
- Sucursales:
  * Punta Carretas: {NEGOCIO['locales']['Punta Carretas']}
  * Tres Cruces: {NEGOCIO['locales']['Tres Cruces']}
  * Centro: {NEGOCIO['locales']['Centro']}
- Reservas: {NEGOCIO['reservas']}

SERVICIOS Y PRECIOS:
{NEGOCIO['servicios']}

INSTRUCCIONES GENERALES:
- Respondé siempre en español rioplatense (vos, che, etc.)
- Sé amigable pero conciso — mensajes cortos y claros, ideales para WhatsApp
- Si alguien saluda, saludá y preguntá en qué podés ayudar
- Usá emojis con moderación — el tono es cálido y relajante, acorde a un spa
- Nunca inventes precios o información que no tenés
- Nunca uses listas largas ni texto en bloque — dividí en mensajes cortos y directos

PRECIOS Y CONSULTAS ECONÓMICAS:
- Si alguien pregunta por precios, dales la información disponible con confianza
- Para zonas de depilación pequeñas (axilas, bozo, mentón, bikini línea, etc.) el precio es $790
- Para zonas mayores o paquetes, decí que los precios varían según el tratamiento y ofrecé confirmar con el equipo o invitales a consultar en local
- No digas simplemente "contactanos para precios" — siempre intentá dar al menos un rango o referencia útil

RESERVAS:
- Si alguien quiere reservar un turno, pedile amablemente: nombre, servicio, fecha preferida, horario y sucursal
- Cuando confirmés una reserva con todos esos datos, incluí la palabra RESERVA_CONFIRMADA en tu respuesta (el cliente no la ve, es solo para el sistema)
- Si faltan datos para completar la reserva, pedí solo lo que falta, no todo de nuevo

MANEJO DE QUEJAS Y SITUACIONES DIFÍCILES:
- Si un cliente expresa una queja o insatisfacción, reconocé el inconveniente con empatía antes de cualquier otra cosa
- No te pongas a la defensiva — validá lo que sienten y ofrecé soluciones concretas
- Ejemplo: "Entiendo que eso no debería haber pasado, lo lamentamos. Dejame ver cómo podemos resolverlo."
- Si la queja es seria o el cliente está muy molesto, derivá al equipo con calidez

DETECCIÓN DE FRUSTRACIÓN:
- Si notás que el cliente repite la misma pregunta, parece impaciente o usa un tono molesto, reconocelo
- En esos casos, ofrecé conectarlos con una persona del equipo: "Parece que no te estoy siendo de mucha ayuda — ¿querés que te ponga en contacto directo con alguien del equipo?"
- No esperes a que el cliente explote — anticipate si ves señales de frustración

CUÁNDO DERIVAR AL EQUIPO:
- Si preguntan algo que no tenés información para responder bien
- Si la situación requiere una decisión que está fuera de tu alcance
- Si el cliente lo pide directamente
- En esos casos decí: "Te paso con el equipo para que te ayuden mejor 🙏"
"""

MENU_BIENVENIDA = f"""¡Hola! 👋 Bienvenida/o a *{NEGOCIO['nombre']}*.

¿En qué te puedo ayudar hoy?

1️⃣ Ver servicios y precios
2️⃣ Reservar un turno
3️⃣ Horarios y sucursales
4️⃣ Hablar con el equipo"""


# ── Base de datos ──────────────────────────────────────────────────────────────
def guardar_mensaje(numero, rol, contenido):
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()
    hora_actual = datetime.now(ZONA_HORARIA).hour
    c.execute("INSERT INTO mensajes (numero, rol, contenido, hora) VALUES (?, ?, ?, ?)", (numero, rol, contenido, hora_actual))

    # Actualizar stats
    c.execute("SELECT id FROM stats WHERE numero = ?", (numero,))
    row = c.fetchone()
    ahora = datetime.now().isoformat()
    if row:
        c.execute("UPDATE stats SET ultimo_contacto = ?, total_mensajes = total_mensajes + 1 WHERE numero = ?", (ahora, numero))
    else:
        c.execute("INSERT INTO stats (numero, primer_contacto, ultimo_contacto, total_mensajes) VALUES (?, ?, ?, 1)", (numero, ahora, ahora))

    conn.commit()
    conn.close()


def obtener_historial(numero, limite=20):
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()
    c.execute("""
        SELECT rol, contenido FROM mensajes
        WHERE numero = ?
        ORDER BY id DESC LIMIT ?
    """, (numero, limite))
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def es_primer_mensaje(numero):
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM mensajes WHERE numero = ?", (numero,))
    count = c.fetchone()[0]
    conn.close()
    return count == 0


def registrar_reserva(numero):
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()
    c.execute("UPDATE stats SET reservas = reservas + 1 WHERE numero = ?", (numero,))
    conn.commit()
    conn.close()


# ── Horario de atención ────────────────────────────────────────────────────────
def esta_abierto():
    ahora = datetime.now(ZONA_HORARIA).time()
    apertura = NEGOCIO["horario_apertura"]
    cierre = NEGOCIO["horario_cierre"]
    # Maneja horario que cruza medianoche (19:00 a 01:00)
    if apertura > cierre:
        return ahora >= apertura or ahora <= cierre
    return apertura <= ahora <= cierre


# ── Notificación al dueño ──────────────────────────────────────────────────────
def notificar_dueno(numero_cliente, detalle_reserva):
    dueno = NEGOCIO.get("numero_dueno", "")
    if not dueno:
        return
    try:
        twilio.messages.create(
            body=f"🔔 Nueva reserva en {NEGOCIO['nombre']}!\n\nCliente: {numero_cliente}\nDetalle: {detalle_reserva}",
            from_=os.getenv("TWILIO_NUMBER"),
            to=dueno,
        )
    except Exception as e:
        print(f"Error notificando al dueño: {e}")


# ── Lógica principal ───────────────────────────────────────────────────────────
def obtener_respuesta(numero, mensaje):
    # Primer mensaje: mostrar menú de bienvenida
    if es_primer_mensaje(numero):
        guardar_mensaje(numero, "user", mensaje)
        guardar_mensaje(numero, "assistant", MENU_BIENVENIDA)
        return MENU_BIENVENIDA

    guardar_mensaje(numero, "user", mensaje)
    historial = obtener_historial(numero)

    respuesta = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=historial,
    )

    texto = respuesta.content[0].text

    # Post-procesado: acortar respuestas largas para WhatsApp
    if len(texto) > 300:
        try:
            respuesta_corta = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system="Sos un asistente que abrevia mensajes de WhatsApp. Recibís un mensaje y lo acortás a menos de 250 caracteres manteniendo la información esencial. Respondé solo con el mensaje acortado, sin explicaciones.",
                messages=[{"role": "user", "content": texto}],
            )
            texto_corto = respuesta_corta.content[0].text.strip()
            if len(texto_corto) < len(texto):
                texto = texto_corto
        except Exception as e:
            print(f"Error al acortar respuesta: {e}")

    # Detectar reserva confirmada y notificar al dueño
    if "RESERVA_CONFIRMADA" in texto:
        texto = texto.replace("RESERVA_CONFIRMADA", "").strip()
        registrar_reserva(numero)
        notificar_dueno(numero, mensaje)

    guardar_mensaje(numero, "assistant", texto)
    return texto


# ── Webhook WhatsApp ───────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    numero = request.form.get("From", "")
    mensaje = request.form.get("Body", "")

    print(f"[{numero}] {mensaje}")

    # Fuera de horario
    if not esta_abierto():
        texto = f"¡Hola! 🌙 Ahora estamos cerrados.\n\nNuestro horario es {NEGOCIO['horario']}.\n\nTe respondemos cuando abramos. ¡Gracias!"
        guardar_mensaje(numero, "user", mensaje)
        guardar_mensaje(numero, "assistant", texto)
        resp = MessagingResponse()
        resp.message(texto)
        return Response(str(resp), mimetype="text/xml")

    respuesta_texto = obtener_respuesta(numero, mensaje)
    print(f"[BOT] {respuesta_texto}")

    resp = MessagingResponse()
    resp.message(respuesta_texto)
    return Response(str(resp), mimetype="text/xml")


# ── Panel de administración ────────────────────────────────────────────────────
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel · {{ negocio }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f0ee; color: #111; min-height: 100vh; }

  /* Header */
  .header { background: #111; color: #fff; padding: 1.1rem 2rem; display: flex; align-items: center; justify-content: space-between; }
  .header-left { display: flex; align-items: center; gap: 0.8rem; }
  .header-dot { width: 8px; height: 8px; background: #c9a84c; border-radius: 50%; }
  .header h1 { font-size: 1rem; font-weight: 700; letter-spacing: 0.5px; }
  .header-sub { color: #888; font-size: 0.75rem; }

  /* Layout */
  .main { max-width: 1100px; margin: 0 auto; padding: 1.8rem 1.5rem; }

  /* KPI Cards */
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
  .kpi { background: #fff; border-radius: 6px; padding: 1.2rem 1.4rem; border-left: 4px solid #ddd; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .kpi.accent-gold { border-left-color: #c9a84c; }
  .kpi.accent-dark { border-left-color: #111; }
  .kpi.accent-green { border-left-color: #4caf82; }
  .kpi.accent-blue { border-left-color: #4c8caf; }
  .kpi .label { font-size: 0.65rem; color: #999; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 0.5rem; }
  .kpi .value { font-size: 2rem; font-weight: 800; color: #111; line-height: 1; }
  .kpi .sub { font-size: 0.7rem; color: #aaa; margin-top: 0.3rem; }

  /* Chart */
  .chart-card { background: #fff; border-radius: 6px; padding: 1.4rem 1.6rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .chart-title { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: #999; margin-bottom: 1.2rem; }
  .chart-bars { display: flex; align-items: flex-end; gap: 8px; height: 100px; }
  .bar-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; height: 100%; }
  .bar-wrap { flex: 1; width: 100%; display: flex; align-items: flex-end; }
  .bar { width: 100%; background: #111; border-radius: 3px 3px 0 0; transition: background 0.2s; min-height: 2px; }
  .bar.today { background: #c9a84c; }
  .bar-label { font-size: 0.6rem; color: #aaa; white-space: nowrap; }
  .bar-val { font-size: 0.65rem; font-weight: 700; color: #555; }

  /* Section */
  .section-title { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; color: #999; margin-bottom: 0.8rem; }

  /* Conversation cards */
  .conv-list { display: flex; flex-direction: column; gap: 0.5rem; }
  .conv-card { background: #fff; border-radius: 6px; padding: 1rem 1.2rem; display: flex; align-items: center; gap: 1rem; text-decoration: none; color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid transparent; transition: border-color 0.15s, box-shadow 0.15s; }
  .conv-card:hover { border-color: #c9a84c; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  .conv-avatar { width: 40px; height: 40px; border-radius: 50%; background: #111; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 0.8rem; font-weight: 700; flex-shrink: 0; }
  .conv-body { flex: 1; min-width: 0; }
  .conv-num { font-size: 0.85rem; font-weight: 600; margin-bottom: 0.2rem; }
  .conv-preview { font-size: 0.78rem; color: #888; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .conv-meta { text-align: right; flex-shrink: 0; }
  .conv-time { font-size: 0.7rem; color: #aaa; margin-bottom: 0.3rem; }
  .conv-badges { display: flex; gap: 0.4rem; justify-content: flex-end; }
  .badge { font-size: 0.6rem; font-weight: 700; padding: 0.15rem 0.45rem; border-radius: 99px; }
  .badge-msgs { background: #f0f0f0; color: #555; }
  .badge-reserva { background: #fff3cd; color: #856404; }

  /* Empty state */
  .empty { text-align: center; padding: 3rem 1rem; color: #aaa; font-size: 0.85rem; }
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <div class="header-dot"></div>
    <h1>{{ negocio }}</h1>
  </div>
  <span class="header-sub">Panel de administración</span>
</div>

<div class="main">

  <!-- KPI Cards -->
  <div class="kpis">
    <div class="kpi accent-dark">
      <div class="label">Conversaciones totales</div>
      <div class="value">{{ stats.contactos }}</div>
      <div class="sub">contactos únicos</div>
    </div>
    <div class="kpi accent-blue">
      <div class="label">Mensajes hoy</div>
      <div class="value">{{ stats.hoy }}</div>
      <div class="sub">en el día de hoy</div>
    </div>
    <div class="kpi accent-green">
      <div class="label">Activos esta semana</div>
      <div class="value">{{ stats.activos_semana }}</div>
      <div class="sub">últimos 7 días</div>
    </div>
    <div class="kpi accent-gold">
      <div class="label">Total mensajes</div>
      <div class="value">{{ stats.total }}</div>
      <div class="sub">{{ stats.reservas }} reservas confirmadas</div>
    </div>
  </div>

  <!-- Bar Chart -->
  <div class="chart-card">
    <div class="chart-title">Mensajes por día — últimos 7 días</div>
    <div class="chart-bars">
      {% for i in range(stats.chart_dias | length) %}
      {% set pct = (stats.chart_vals[i] / stats.chart_max * 100) | int %}
      <div class="bar-col">
        <div class="bar-val">{{ stats.chart_vals[i] if stats.chart_vals[i] > 0 else '' }}</div>
        <div class="bar-wrap">
          <div class="bar {% if loop.last %}today{% endif %}" style="height: {{ [pct, 2] | max }}%;"></div>
        </div>
        <div class="bar-label">{{ stats.chart_dias[i] }}</div>
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- Conversations -->
  <div class="section-title">Conversaciones recientes</div>
  <div class="conv-list">
    {% if conversaciones %}
      {% for c in conversaciones %}
      <a class="conv-card" href="/admin/chat/{{ c.numero_enc }}">
        <div class="conv-avatar">{{ c.numero[-2:] }}</div>
        <div class="conv-body">
          <div class="conv-num">{{ c.numero }}</div>
          <div class="conv-preview">{{ c.ultimo_msg or 'Sin mensajes' }}</div>
        </div>
        <div class="conv-meta">
          <div class="conv-time">{{ c.ultimo_contacto }}</div>
          <div class="conv-badges">
            <span class="badge badge-msgs">{{ c.total_mensajes }} msgs</span>
            {% if c.reservas > 0 %}
            <span class="badge badge-reserva">{{ c.reservas }} reserva{{ 's' if c.reservas > 1 else '' }}</span>
            {% endif %}
          </div>
        </div>
      </a>
      {% endfor %}
    {% else %}
      <div class="empty">Todavía no hay conversaciones registradas.</div>
    {% endif %}
  </div>

</div>
</body>
</html>
"""

CHAT_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Chat · {{ numero }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #f5f5f5; color: #111; }
  .header { background: #111; color: #fff; padding: 1rem 2rem; }
  .header a { color: #c9a84c; font-size: 0.8rem; }
  .header h2 { font-size: 1rem; margin-top: 0.3rem; }
  .chat { padding: 1.5rem 2rem; max-width: 700px; margin: 0 auto; }
  .msg { margin-bottom: 1rem; }
  .msg.user .bubble { background: #f0f0f0; border-radius: 12px 12px 12px 2px; padding: 0.6rem 1rem; display: inline-block; max-width: 80%; }
  .msg.assistant .bubble { background: #111; color: #fff; border-radius: 12px 12px 2px 12px; padding: 0.6rem 1rem; display: inline-block; max-width: 80%; white-space: pre-wrap; }
  .msg.assistant { text-align: right; }
  .ts { font-size: 0.65rem; color: #aaa; margin-top: 0.3rem; }
</style>
</head>
<body>
<div class="header">
  <a href="/admin">← Volver</a>
  <h2>{{ numero }}</h2>
</div>
<div class="chat">
  {% for m in mensajes %}
  <div class="msg {{ m.rol }}">
    <div class="bubble">{{ m.contenido }}</div>
    <div class="ts">{{ m.timestamp }}</div>
  </div>
  {% endfor %}
</div>
</body>
</html>
"""


@app.route("/admin")
def admin():
    import urllib.parse

    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()

    c.execute("SELECT COUNT(DISTINCT numero) FROM stats")
    contactos = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM mensajes WHERE date(timestamp) = date('now')")
    hoy = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT numero) FROM mensajes WHERE timestamp >= datetime('now', '-7 days')")
    activos_semana = c.fetchone()[0]

    c.execute("SELECT SUM(reservas) FROM stats")
    reservas = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM mensajes")
    total = c.fetchone()[0]

    # Mensajes por dia (ultimos 7 dias) para el grafico
    c.execute("""
        SELECT date(timestamp) as dia, COUNT(*) as cnt
        FROM mensajes
        WHERE timestamp >= datetime('now', '-6 days')
        GROUP BY dia
        ORDER BY dia ASC
    """)
    dias_raw = {r[0]: r[1] for r in c.fetchall()}

    # Construir lista de los ultimos 7 dias garantizando todos los dias
    from datetime import timedelta
    hoy_fecha = datetime.now(ZONA_HORARIA).date()
    chart_dias = []
    chart_vals = []
    for i in range(6, -1, -1):
        d = hoy_fecha - timedelta(days=i)
        chart_dias.append(d.strftime("%d/%m"))
        chart_vals.append(dias_raw.get(str(d), 0))

    chart_max = max(chart_vals) if max(chart_vals) > 0 else 1

    # Conversaciones con ultimo mensaje preview
    c.execute("""
        SELECT s.numero, s.primer_contacto, s.ultimo_contacto, s.total_mensajes, s.reservas,
               (SELECT contenido FROM mensajes WHERE numero = s.numero ORDER BY id DESC LIMIT 1) as ultimo_msg
        FROM stats s
        ORDER BY s.ultimo_contacto DESC
    """)
    rows = c.fetchall()
    conn.close()

    conversaciones = [{
        "numero": r[0],
        "numero_enc": urllib.parse.quote(r[0], safe=""),
        "primer_contacto": r[1][:16] if r[1] else "",
        "ultimo_contacto": r[2][:16] if r[2] else "",
        "total_mensajes": r[3],
        "reservas": r[4],
        "ultimo_msg": (r[5][:60] + "…") if r[5] and len(r[5]) > 60 else (r[5] or ""),
    } for r in rows]

    stats = {
        "contactos": contactos,
        "hoy": hoy,
        "activos_semana": activos_semana,
        "reservas": reservas,
        "total": total,
        "chart_dias": chart_dias,
        "chart_vals": chart_vals,
        "chart_max": chart_max,
    }
    return render_template_string(ADMIN_HTML, negocio=NEGOCIO["nombre"], stats=stats, conversaciones=conversaciones)


@app.route("/admin/chat/<path:numero>")
def admin_chat(numero):
    import urllib.parse
    numero = urllib.parse.unquote(numero)
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()
    c.execute("SELECT rol, contenido, timestamp FROM mensajes WHERE numero = ? ORDER BY id", (numero,))
    rows = c.fetchall()
    conn.close()
    mensajes = [{"rol": r[0], "contenido": r[1], "timestamp": r[2][:16] if r[2] else ""} for r in rows]
    return render_template_string(CHAT_HTML, numero=numero, mensajes=mensajes)


@app.route("/")
def index():
    return f"Bot de {NEGOCIO['nombre']} funcionando ✓ · <a href='/admin'>Panel admin</a>"


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
