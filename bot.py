import os
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

app = Flask(__name__)
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Memoria de conversaciones por número de teléfono
conversaciones = {}

# ── Configuración del negocio (se personaliza para cada cliente) ──────────────
NEGOCIO = {
    "nombre": "Fundamento Pizza",
    "tipo": "pizzería artesanal",
    "locales": {
        "Pocitos": "Av. Brasil 2715, tel: 093 551 815",
        "Cordón": "Canelones 1890, tel: 093 551 815",
        "Ciudad Vieja": "Piedras 402, tel: 093 551 815",
    },
    "horario": "todos los días de 19:00 a 01:00",
    "menu": """
    🍕 PIZZAS ARTESANALES
    - Margherita: $450
    - Pepperoni: $520
    - Funghi: $490
    - Quattro Formaggi: $550
    - Especial Fundamento (rúcula, jamón crudo, parmesano): $590

    🥗 ENTRADAS
    - Focaccia con aceite de oliva: $180
    - Bruschetta x3: $220

    🍺 BEBIDAS
    - Cerveza artesanal: $180
    - Vino copa: $200
    - Gaseosa: $100
    - Agua: $80
    """,
    "reservas": "Se pueden hacer reservas para grupos de 4 o más personas.",
    "delivery": "No hacemos delivery, solo para llevar o comer en el local.",
}

SYSTEM_PROMPT = f"""Sos el asistente virtual de {NEGOCIO['nombre']}, una {NEGOCIO['tipo']} en Montevideo, Uruguay.

Tu trabajo es atender a los clientes por WhatsApp de forma amigable, rápida y profesional.

INFORMACIÓN DEL NEGOCIO:
- Nombre: {NEGOCIO['nombre']}
- Horario: {NEGOCIO['horario']}
- Locales:
  * Pocitos: {NEGOCIO['locales']['Pocitos']}
  * Cordón: {NEGOCIO['locales']['Cordón']}
  * Ciudad Vieja: {NEGOCIO['locales']['Ciudad Vieja']}
- Delivery: {NEGOCIO['delivery']}
- Reservas: {NEGOCIO['reservas']}

MENÚ:
{NEGOCIO['menu']}

INSTRUCCIONES:
- Respondé siempre en español rioplatense (vos, che, etc.)
- Sé amigable pero conciso — mensajes cortos y claros
- Si alguien quiere hacer una reserva, pedile: nombre, fecha, hora, cantidad de personas y local
- Si preguntan algo que no sabés, decí: "Te paso con el equipo para que te ayuden mejor 🙏"
- Usá emojis con moderación
- Nunca inventes precios o información que no tenés
- Si alguien saluda, saludá y preguntá en qué podés ayudar
"""


def obtener_respuesta(numero, mensaje):
    if numero not in conversaciones:
        conversaciones[numero] = []

    conversaciones[numero].append({"role": "user", "content": mensaje})

    # Mantener solo los últimos 10 mensajes para no gastar tokens
    historial = conversaciones[numero][-10:]

    respuesta = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=historial,
    )

    texto = respuesta.content[0].text
    conversaciones[numero].append({"role": "assistant", "content": texto})

    return texto


@app.route("/webhook", methods=["POST"])
def webhook():
    numero = request.form.get("From", "")
    mensaje = request.form.get("Body", "")

    print(f"[{numero}] {mensaje}")

    respuesta_texto = obtener_respuesta(numero, mensaje)

    print(f"[BOT] {respuesta_texto}")

    resp = MessagingResponse()
    resp.message(respuesta_texto)
    return Response(str(resp), mimetype="text/xml")


@app.route("/", methods=["GET"])
def index():
    return f"Bot de {NEGOCIO['nombre']} funcionando ✓"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
