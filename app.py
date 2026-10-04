import base64
import os
import re
import time
from pathlib import Path

import streamlit as st
from google import genai
from google.genai import types
import requests

from prompts import STYLORA_SYSTEM_PROMPT



st.set_page_config(page_title="Stylora", page_icon="👗", layout="wide")

def _read_secret_value(key_name: str) -> str:
    """Read a secret from Streamlit, local files, or environment variables."""
    try:
        value = st.secrets.get(key_name)
        if value:
            return str(value)
    except Exception:
        pass

    base_dir = Path(__file__).resolve().parent
    candidates = [
        base_dir / ".streamlit" / "secrets.toml",
        base_dir / "streamlit" / "secrets.toml",
        base_dir / "secrets.toml",
    ]

    for secret_path in candidates:
        if not secret_path.exists():
            continue

        try:
            import tomllib

            with secret_path.open("rb") as f:
                data = tomllib.load(f)
            value = data.get(key_name)
            if value:
                return str(value)
        except Exception:
            pass

        try:
            raw_text = secret_path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(rf'{re.escape(key_name)}\s*=\s*["\']?([^"\n\r]+)["\']?', raw_text)
            if match:
                value = match.group(1).strip()
                if value:
                    return str(value)
        except Exception:
            pass

    return str(os.getenv(key_name) or "")


def get_google_api_key() -> str:
    """Try Streamlit secrets, local secret files, and environment variables."""
    return _read_secret_value("GOOGLE_API_KEY")


def get_gemini_model() -> str:
    """Return the preferred Gemini model, preferring the current API-recommended 3.8 model."""
    candidates = get_gemini_model_candidates()
    return candidates[0] if candidates else "gemini-3.8-flash"


def get_gemini_model_candidates() -> list[str]:
    """Return ordered Gemini model candidates with the current API-recommended model first."""
    configured = (_read_secret_value("GEMINI_MODEL") or os.getenv("GEMINI_MODEL") or "").strip()
    preferred = [configured] if configured else []
    fallback = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
    candidates = []
    for model_name in preferred + fallback:
        if model_name and model_name not in candidates:
            candidates.append(model_name)
    return candidates or ["gemini-3.8-flash"]



def extract_text(response):
    if hasattr(response, "text") and response.text:
        return response.text.strip()

    try:
        candidates = getattr(response, "candidates", [])
        if candidates:
            parts = getattr(candidates[0].content, "parts", [])
            if parts and hasattr(parts[0], "text"):
                return parts[0].text.strip()
    except Exception:
        pass

    return str(response).strip()


def _is_placeholder_value(value: str) -> bool:
    if value is None:
        return True
    cleaned = str(value).strip()
    if not cleaned:
        return True
    normalized = cleaned.lower().replace(" ", "")
    placeholder_markers = {
        "your_telegram_bot_token",
        "your_telegram_chat_id",
        "your_bot_token",
        "your_chat_id",
        "example",
        "changeme",
        "placeholder",
        "dummy",
    }
    return normalized in placeholder_markers or normalized.startswith("your_") or normalized.startswith("example")


def get_telegram_config() -> dict:
    config = {}
    for key in [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ADMIN_CHAT_ID",
    ]:
        value = _read_secret_value(key)
        if value and not _is_placeholder_value(value):
            config[key] = str(value).strip()
    return config


def send_telegram_message(message: str, chat_id: str = "") -> str:
    telegram_cfg = get_telegram_config()
    bot_token = telegram_cfg.get("TELEGRAM_BOT_TOKEN")
    target_chat_id = (
        chat_id
        or telegram_cfg.get("TELEGRAM_CHAT_ID")
        or telegram_cfg.get("ADMIN_CHAT_ID")
        or ""
    )

    if not bot_token:
        return "Telegram bot token is not configured. Add TELEGRAM_BOT_TOKEN in your secrets file before sending Telegram messages."

    if not target_chat_id:
        return "Add a Telegram chat ID in the form or configure TELEGRAM_CHAT_ID/ADMIN_CHAT_ID before sending."

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message, "parse_mode": "HTML"}

    try:
        response = requests.post(url, data=payload, timeout=20)
        response.raise_for_status()
        return "Telegram message sent successfully."
    except Exception as exc:
        return f"Telegram send failed: {exc}"


def _should_retry_gemini_error(exc: Exception) -> bool:
    message = str(exc).lower()
    retry_tokens = [
        "503",
        "429",
        "rate limit",
        "resource_exhausted",
        "temporarily unavailable",
        "unavailable",
        "timeout",
        "busy",
        "quota",
        "overloaded",
    ]
    return any(token in message for token in retry_tokens)


def _call_gemini_with_retry(client, model: str, contents, max_retries: int = 3):
    last_exc = None
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except Exception as exc:
            last_exc = exc
            if not _should_retry_gemini_error(exc) or attempt == max_retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Gemini request failed without an exception payload.")


 

def ask_gemini(question: str, image_file=None, mode: str = "styling") -> str:
    api_key = get_google_api_key()
    if not api_key:
        return (
            "Please add your Gemini API key to `.streamlit/secrets.toml` or set the "
            "`GOOGLE_API_KEY` environment variable before using Stylora."
        )

    model_name = get_gemini_model()
    client = genai.Client(api_key=api_key)

    parts = [
        types.Part.from_text(text=STYLORA_SYSTEM_PROMPT),
        types.Part.from_text(
            text=(
                "Use the uploaded image as the outfit context and answer the user's request. "
                "Be concise, practical and avoid guessing anything not visible."
            )
        ),
    ]

    if image_file is not None:
        image_bytes = image_file.getvalue()
        parts.append(
            types.Part.from_bytes(
                data=image_bytes,
                mime_type=image_file.type or "image/jpeg",
            )
        )

    if st.session_state.get("current_outfit"):
        parts.append(
            types.Part.from_text(
                text=(
                    "Remember this outfit context from earlier in the conversation: "
                    f"{st.session_state.current_outfit}"
                )
            )
        )

    if mode == "telegram":
        parts.append(
            types.Part.from_text(
                text=(
                    "Create a concise Telegram-ready message only. Use the fashion recommendations "
                    "that are visible in the uploaded outfit and the question asked by the user."
                )
            )
        )

    parts.append(types.Part.from_text(text=f"User question: {question}"))

    model_candidates = get_gemini_model_candidates()
    last_exc = None

    for model_name in model_candidates:
        try:
            response = _call_gemini_with_retry(client, model_name, parts)
            return extract_text(response)
        except Exception as exc:
            last_exc = exc
            details = str(exc)
            if "404" in details or "NOT_FOUND" in details.upper() or "not found for API version" in details.lower():
                continue
            if "429" in details or "quota" in details.lower() or "resource_exhausted" in details.lower():
                return (
                    "The Gemini API is currently rate-limited or quota-exhausted. Please wait a moment and try again. "
                    f"Technical details: {details}"
                )
            return (
                "Sorry, I could not generate a response right now. The Gemini service may be busy or temporarily unavailable. "
                f"Please try again in a moment. Technical details: {details}"
            )

    if last_exc is not None:
        details = str(last_exc)
        return (
            "None of the configured Gemini model names were available for this API version. "
            "Please use a supported model like gemini-2.5-flash or gemini-2.0-flash. "
            f"Technical details: {details}"
        )

    return "Sorry, I could not generate a response right now."


if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "current_outfit" not in st.session_state:
    st.session_state.current_outfit = ""
if "whatsapp_message" not in st.session_state:
    st.session_state.whatsapp_message = ""


def render_welcome_message():
    st.markdown(
        """
        ✨ Welcome to Stylora!
        Your personal style companion is here. 👗

        Upload a photo of your outfit and I'll help you:
        • Understand your current look
        • Find colors and pieces that pair well
        • Choose accessories and footwear
        • Adapt your outfit for different occasions
        • Create new styling ideas from what you already own

        📸 Upload your outfit to get started!
        """
    )


render_welcome_message()

api_key = get_google_api_key()
telegram_cfg = get_telegram_config()

st.sidebar.title("Stylora")
st.sidebar.caption("Your image-based fashion styling assistant")
st.sidebar.caption("Telegram bot: @StyloraFashionBot")

with st.sidebar.expander("How to receive messages on Telegram", expanded=False):
    st.markdown(
        """
        1. Open Telegram and search for @BotFather.
        2. Send `/newbot`, choose a name, and copy the bot token it gives you.
        3. Open @userinfobot and send `/start`.
        4. Copy the numeric chat ID it replies with.
        5. In this website, paste that chat ID into the Telegram chat ID field below the generated message.
        6. Click "Send via Telegram" to receive the message in your chat.
        """
    )

if not api_key:
    st.sidebar.info(
        "Add your Gemini API key in `.streamlit/secrets.toml` as `GOOGLE_API_KEY = \"...\"` or set the environment variable."
    )
if not telegram_cfg:
    st.sidebar.warning(
        "Telegram is optional. Add TELEGRAM_BOT_TOKEN and either TELEGRAM_CHAT_ID or ADMIN_CHAT_ID. You can also enter the chat ID in the form."
    )

with st.container():
    uploaded_file = st.file_uploader(
        "Upload an outfit photo",
        type=["png", "jpg", "jpeg", "webp"],
        help="Upload the outfit you want analyzed.",
    )

    if uploaded_file is not None:
        st.image(uploaded_file, caption="Uploaded outfit", width="stretch")

    with st.form("style_form", clear_on_submit=True):
        user_question = st.text_area(
            "Ask a styling question",
            value="",
            placeholder="Example: What shoes would match this? How can I dress this up for a party?",
            height=120,
        )
        submitted = st.form_submit_button("Send")

    if submitted and user_question.strip():
        response = ask_gemini(user_question.strip(), image_file=uploaded_file)
        st.session_state.chat_history.append({"role": "user", "content": user_question.strip()})
        st.session_state.chat_history.append({"role": "assistant", "content": response})
        st.session_state.current_outfit = response
        st.rerun()

    if not api_key:
        st.info("Upload a photo or ask a styling question to begin.")

    if uploaded_file is not None:
        if st.button("Analyze this outfit"):
            response = ask_gemini(
                "Analyze this outfit in detail and suggest styling ideas, footwear, accessories, and occasions.",
                image_file=uploaded_file,
            )
            st.session_state.chat_history.append({"role": "assistant", "content": response})
            st.session_state.current_outfit = response
            st.rerun()

        if st.button("Generate Telegram message"):
            latest_text = ""
            if st.session_state.chat_history:
                latest_text = st.session_state.chat_history[-1]["content"]
            telegram_message = ask_gemini(
                (
                    "Create a short Telegram-ready outfit recommendation using the latest styling "
                    f"advice. Context: {latest_text}"
                ),
                image_file=uploaded_file,
                mode="telegram",
            )
            st.session_state.whatsapp_message = telegram_message
            st.success("Telegram-ready message generated below.")

        if st.session_state.whatsapp_message:
            st.markdown("### Telegram-ready message")
            st.code(st.session_state.whatsapp_message, language="text")

            default_chat_id = telegram_cfg.get("TELEGRAM_CHAT_ID") or telegram_cfg.get("ADMIN_CHAT_ID", "")
            recipient = st.text_input(
                "Telegram chat ID",
                value=default_chat_id,
                help="For deployment, set ADMIN_CHAT_ID in secrets or type the target numeric Telegram chat ID here.",
            )

            if st.button("Send via Telegram"):
                result = send_telegram_message(st.session_state.whatsapp_message, chat_id=recipient)
                st.write(result)

    if st.session_state.chat_history:
        st.markdown("### Conversation")
        for msg in st.session_state.chat_history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if not st.session_state.chat_history and uploaded_file is None:
        st.info("Upload a photo or ask a styling question to begin.")

