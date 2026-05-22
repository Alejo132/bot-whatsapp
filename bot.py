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
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
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

INSTRUCCIONES:
- Respondé siempre en español rioplatense (vos, che, etc.)
- Sé amigable pero conciso — mensajes cortos y claros
- Si alguien quiere reservar un turno, pedile: nombre, servicio, fecha, hora y sucursal
- Cuando confirmés una reserva, incluí la palabra RESERVA_CONFIRMADA en tu respuesta (invisible para el cliente, solo para el sistema)
- Si preguntan algo que no sabés, decí: "Te paso con el equipo para que te ayuden mejor 🙏"
- Usá emojis con moderación, el tono es cálido y relajante acorde a un spa
- Nunca inventes precios o información que no tenés
- Si alguien saluda, saludá y preguntá en qué podés ayudar
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
    c.execute("INSERT INTO mensajes (numero, rol, contenido) VALUES (?, ?, ?)", (numero, rol, contenido))

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


def obtener_historial(numero, limite=10):
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
  body { font-family: 'Segoe UI', sans-serif; background: #f5f5f5; color: #111; }
  .header { background: #111; color: #fff; padding: 1.2rem 2rem; display: flex; align-items: center; gap: 1rem; }
  .header h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: 1px; }
  .header span { color: #c9a84c; font-size: 0.8rem; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; padding: 1.5rem 2rem 0; }
  .kpi { background: #fff; border-top: 3px solid #111; padding: 1rem 1.2rem; border-radius: 2px; }
  .kpi .label { font-size: 0.65rem; color: #999; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 0.4rem; }
  .kpi .value { font-size: 1.8rem; font-weight: 700; }
  .kpi.gold { border-top-color: #c9a84c; }
  .section { padding: 1.5rem 2rem; }
  .section h2 { font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; border-bottom: 2px solid #111; padding-bottom: 0.5rem; margin-bottom: 1rem; }
  table { width: 100%; background: #fff; border-collapse: collapse; font-size: 0.85rem; }
  th { background: #111; color: #fff; padding: 0.6rem 0.8rem; text-align: left; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; }
  td { padding: 0.6rem 0.8rem; border-bottom: 1px solid #f0f0f0; }
  tr:nth-child(even) td { background: #fafafa; }
  .chat { background: #fff; border-radius: 2px; padding: 1rem; max-height: 400px; overflow-y: auto; }
  .msg { margin-bottom: 0.8rem; }
  .msg.user .bubble { background: #f0f0f0; border-radius: 12px 12px 12px 2px; padding: 0.5rem 0.8rem; display: inline-block; max-width: 80%; }
  .msg.assistant .bubble { background: #111; color: #fff; border-radius: 12px 12px 2px 12px; padding: 0.5rem 0.8rem; display: inline-block; max-width: 80%; float: right; }
  .msg.assistant { text-align: right; }
  .clearfix::after { content: ""; display: table; clear: both; }
  .ts { font-size: 0.65rem; color: #aaa; margin-top: 0.2rem; }
  select, input { padding: 0.4rem 0.6rem; border: 1px solid #ddd; border-radius: 2px; font-size: 0.85rem; margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="header">
  <h1>{{ negocio }}</h1>
  <span>Panel de administración</span>
</div>

<div class="kpis">
  <div class="kpi"><div class="label">Contactos únicos</div><div class="value">{{ stats.contactos }}</div></div>
  <div class="kpi"><div class="label">Mensajes hoy</div><div class="value">{{ stats.hoy }}</div></div>
  <div class="kpi gold"><div class="label">Reservas</div><div class="value">{{ stats.reservas }}</div></div>
  <div class="kpi"><div class="label">Total mensajes</div><div class="value">{{ stats.total }}</div></div>
</div>

<div class="section">
  <h2>Conversaciones</h2>
  <table>
    <tr><th>Número</th><th>Primer contacto</th><th>Último contacto</th><th>Mensajes</th><th>Reservas</th></tr>
    {% for c in conversaciones %}
    <tr>
      <td><a href="/admin/chat/{{ c.numero_enc }}">{{ c.numero }}</a></td>
      <td>{{ c.primer_contacto }}</td>
      <td>{{ c.ultimo_contacto }}</td>
      <td>{{ c.total_mensajes }}</td>
      <td>{{ c.reservas }}</td>
    </tr>
    {% endfor %}
  </table>
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
    conn = sqlite3.connect("conversaciones.db")
    c = conn.cursor()

    c.execute("SELECT COUNT(DISTINCT numero) FROM stats")
    contactos = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM mensajes WHERE date(timestamp) = date('now')")
    hoy = c.fetchone()[0]

    c.execute("SELECT SUM(reservas) FROM stats")
    reservas = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM mensajes")
    total = c.fetchone()[0]

    c.execute("SELECT numero, primer_contacto, ultimo_contacto, total_mensajes, reservas FROM stats ORDER BY ultimo_contacto DESC")
    rows = c.fetchall()
    conn.close()

    import urllib.parse
    conversaciones = [{
        "numero": r[0],
        "numero_enc": urllib.parse.quote(r[0], safe=""),
        "primer_contacto": r[1][:16] if r[1] else "",
        "ultimo_contacto": r[2][:16] if r[2] else "",
        "total_mensajes": r[3],
        "reservas": r[4],
    } for r in rows]

    stats = {"contactos": contactos, "hoy": hoy, "reservas": reservas, "total": total}
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
