from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI
import os

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_prompt(character):
    try:
        with open(os.path.join(BASE_DIR, "prompts", "generic_prompt.md"), "r", encoding="utf-8") as f:
            general = f.read()
        with open(os.path.join(BASE_DIR, "characters", f"{character}.md"), "r", encoding="utf-8") as f:
            specific = f.read()
        return f"{general}\n\n{specific}"
    except FileNotFoundError:
        return None

@app.route("/about")
def about():
    try:
        with open("about.txt", "r", encoding="utf-8") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return "No About content found."

@app.route("/stt", methods=["POST"])
def speech_to_text():
    from openai import OpenAI
    import uuid
    import os

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    os.makedirs("temp", exist_ok=True)
    temp_filename = f"temp/input_{uuid.uuid4()}.webm"
    file.save(temp_filename)

    try:
        client = OpenAI()
        with open(temp_filename, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text"
            )
        return jsonify({"transcript": transcript.strip()})
    finally:
        os.remove(temp_filename)

@app.route("/tts", methods=["POST"])
def generate_tts():
    from openai import OpenAI
    from flask import Response

    data = request.json
    text = data.get("text", "")
    voice = data.get("voice", "shimmer")

    if not text:
        return jsonify({"error": "No text provided."}), 400

    client = OpenAI()
    response = client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text
    )
    return Response(response.content, mimetype="audio/mpeg")


@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.json
    character = data.get("character")
    user_input = data.get("user_input")

    if not character or not user_input:
        return jsonify({"error": "Missing character or user_input"}), 400

    system_prompt = load_prompt(character)
    if not system_prompt:
        return jsonify({"error": f"Character '{character}' not found."}), 404

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0.8
        )

        message = response.choices[0].message.content.strip()
        usage = response.usage
        cost_estimate = (usage.prompt_tokens * 0.000005) + (usage.completion_tokens * 0.000015)

        return jsonify({
            "reply": message,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "estimated_cost": round(cost_estimate, 5)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

