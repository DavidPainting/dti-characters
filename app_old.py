
from flask import Flask, request, render_template_string
import openai
import os
from openai import OpenAI


app = Flask(__name__)

# Set this via environment variable or manually insert your key
openai.api_key = os.getenv("OPENAI_API_KEY", "sk-REPLACE_WITH_YOUR_KEY")

BASE_DIR = "."

def load_prompt(character):
    try:
        with open(f"{BASE_DIR}/prompts/generic_prompt.md", "r", encoding="utf-8") as f:
            general = f.read()
        with open(f"{BASE_DIR}/characters/{character}.md", "r", encoding="utf-8") as f:
            specific = f.read()
        return f"{general}\n\n{specific}"
    except FileNotFoundError:
        return None

@app.route("/", methods=["GET"])
def index():
    with open(f"{BASE_DIR}/static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.route("/ask", methods=["POST"])
def ask():
    character = request.form.get("character")
    user_input = request.form.get("user_input")

    system_prompt = load_prompt(character)
    if not system_prompt:
        return f"Character '{character}' not found.", 404

    try:
        client = openai.OpenAI()

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0.8
        )

        reply = response.choices[0].message.content.strip()
        # Token usage
        tokens = response.usage
        prompt_tokens = tokens.prompt_tokens
        completion_tokens = tokens.completion_tokens
        total_tokens = tokens.total_tokens

        # Cost estimate for GPT-4o ($5 / $15 per million tokens)
        cost_estimate = (prompt_tokens * 0.000005) + (completion_tokens * 0.000015)


    except Exception as e:
        reply = f"Error contacting OpenAI: {str(e)}"

    return render_template_string(f'''
        <html>
        <head>
            <title>{character.capitalize()} Responds</title>
        </head>
        <body style="font-family: Georgia, serif; line-height: 1.5; padding: 2em; background: #f9f9f9;">
            <h2>{character.capitalize()}</h2>
            <img src="/static/images/{character}.jpg" alt="{character} portrait"
                 style="float: right; max-height: 150px; margin: 1em; border-radius: 8px;">
            <p><strong>Your question:</strong></p>
            <div style="padding: 1em; border: 1px solid #ccc; background: #fff; margin-bottom: 2em;">
                {user_input}
            </div>
            <p><strong>{character.capitalize()} replies:</strong></p>
            <div style="border: 1px solid #ccc; padding: 1em; background: #fdfdfd; max-height: 300px; overflow-y: auto;">
                {reply}
            </div>
            <hr>
            <p><small>🧾 Token usage: {total_tokens} (Prompt: {prompt_tokens}, Response: {completion_tokens})<br>
            💸 Estimated cost: ${cost_estimate:.5f}</small></p>
            <p><a href="/">Ask another question</a></p>
        </body>
        </html>
    ''')


if __name__ == "__main__":
    app.run(debug=True)
