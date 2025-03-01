import eel
import requests
import json
import logging
import logging.handlers
import os
import webbrowser
import time
import psutil
import pynvml
import threading
import ctypes
import sys

if getattr(sys, "frozen", False):
    import pyi_splash


# runtime error message if no supported browser is found
def show_missing_runtime_error():
    MB_YESNOCANCEL = 0x03
    MB_ICONERROR = 0x10
    MB_DEFBUTTON3 = 0x200
    IDYES = 6
    IDNO = 7

    message = (
        "No supported browser found!\n\n"
        "OllamaTor requires Google Chrome or Microsoft Edge.\n\n"
        "Please install one of these browsers and try again.\n\n"
        "Click [Yes] for Chrome or [No] for Edge, [Cancel] or 'x' to exit.\n\n"
        "(You will be redirected to the browser download page.)"
    )
    title = "OllamaTor - RuntimeError"

    # MessageBoxW aufrufen
    result = ctypes.windll.user32.MessageBoxW(
        0,
        message,
        title,
        MB_YESNOCANCEL | MB_ICONERROR | MB_DEFBUTTON3,
    )

    # Ergebnis auswerten
    if result == IDYES:
        webbrowser.open_new_tab("https://www.google.com/chrome/")
    elif result == IDNO:
        webbrowser.open_new_tab("https://www.microsoft.com/edge/")


MAIN_DIR = ".OllamaTor"
if not os.path.exists(MAIN_DIR):
    os.makedirs(MAIN_DIR)

# logging system for troubleshooting
LOG_DIR = MAIN_DIR + "/logs"
LOG_SIZE_LIMIT = 1024 * 1024  # 1 MB
LOG_BACKUP_COUNT = 5

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
LOG_PATH = os.path.join(LOG_DIR, "OllamaTor.log")


# Erstelle einen RotatingFileHandler
file_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH,
    maxBytes=LOG_SIZE_LIMIT,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
)
file_handler.setLevel(logging.INFO)


formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)
logging.getLogger().setLevel(logging.INFO)

eel.init("src")

message_history = []

model_cache = []
cache_timestamp = 0
CACHE_DURATION = 1


# get the available models from the local AI server
@eel.expose
def get_available_models_cached():
    global model_cache, cache_timestamp
    current_time = time.time()

    if model_cache and (current_time - cache_timestamp) < CACHE_DURATION:
        return model_cache

    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        response.raise_for_status()
        data = response.json()
        models = [m.get("name", "Unknown") for m in data.get("models", [])]
        model_cache = models
        cache_timestamp = current_time
        logging.info("Local AI models: %s", models)
        return models
    except requests.exceptions.RequestException as e:
        logging.error(
            "Error while requesting local AI models (RequestException): %s", e
        )
        return []
    except Exception as e:
        logging.error("Error while requesting local AI models (Exception): %s", e)
        return []


# get the ai speed/tpm after finished response
def calculate_tps(data, tps_type="generation"):
    if tps_type == "total":
        if data["total_duration"] == 0:
            return 0.0
        total_tokens = data["prompt_eval_count"] + data["eval_count"]
        return total_tokens / (data["total_duration"] / 1_000_000_000.0)
    elif tps_type == "prompt":
        if data["prompt_eval_duration"] == 0:
            return 0.0
        return data["prompt_eval_count"] / (
            data["prompt_eval_duration"] / 1_000_000_000.0
        )
    else:
        if data["eval_duration"] == 0:
            return 0.0
        return data["eval_count"] / (data["eval_duration"] / 1_000_000_000.0)


# generate resonse - using a thread for better performance
def generate_response_thread(model, messages, temperature):
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    full_response = ""

    try:
        with requests.post(url, json=payload, stream=True, timeout=60) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if line:
                    try:
                        part = json.loads(line)
                    except Exception as json_err:
                        logging.error("Error while decoding JSON: %s", json_err)
                        continue

                    if "message" in part and "content" in part["message"]:
                        content = part["message"]["content"]
                    elif "response" in part:
                        content = part["response"]
                    else:
                        content = ""
                    full_response += content
                    eel.update_chat_message(full_response)()

                    if part.get("done"):
                        tps = calculate_tps(part, tps_type="generation")
                        eel.update_tps(f"{tps:.2f} TPS")()

            eel.appendMessage("assistant", full_response)()

    except Exception as e:
        logging.error("Error in generate_response_thread(): %s", e)
        eel.update_chat_message("❌ Error while generating response.")()


# send the prompt to the AI server
@eel.expose
def generate_response(model, prompt, temperature, historyLength):
    global message_history
    logging.info(
        "generate_response() running - Model: %s, Prompt: %s, Temperature: %s, History-Length: %s",
        model,
        prompt,
        temperature,
        historyLength,
    )

    message_history.append({"role": "user", "content": prompt})
    if historyLength < 20:
        messages_to_send = (
            message_history[-historyLength:]
            if historyLength > 0
            else [{"role": "user", "content": prompt}]
        )
    else:
        messages_to_send = message_history

    thread = threading.Thread(
        target=generate_response_thread,
        args=(model, messages_to_send, temperature),
        daemon=True,
    )
    thread.start()

    return {"value": "OK"}


# download the OllamaSetup.exe
@eel.expose
def download_ollama():
    try:
        temp_dir = "./temp"
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        filepath = os.path.join(temp_dir, "OllamaSetup.exe")
        if not os.path.isfile(filepath):
            logging.info("OllamaSetup.exe not installed. Starting download...")
            try:
                with requests.get(
                    "https://ollama.com/download/OllamaSetup.exe",
                    stream=True,
                    timeout=(10, 60),
                ) as r:
                    r.raise_for_status()
                    total_size = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    with open(filepath, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total_size > 0:
                                    progress = downloaded / total_size
                                    logging.info(f"Downloading... {progress:.2%}")
            except requests.exceptions.RequestException as e:
                logging.error(f"Network error: {e}")
                return
            except Exception as e:
                logging.error(f"An unexpected error occurred during download: {e}")
                return
            logging.info("Download complete.")
        else:
            logging.info("OllamaSetup.exe is already installed, starting Setup...")
        eel.start_setup()
        os.startfile(filepath)

    except Exception as e:
        logging.error(f"Error in download_ollama: {e}")


# open a link in the default browser
@eel.expose
def open_new_tab(url):
    try:
        webbrowser.open_new_tab(url)
    except Exception as e:
        logging.info(f"Error in open_new_tab: {e}")


# get the system resources/system usage
@eel.expose
def get_system_resources():
    cpu_percent = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    ram_percent = ram.percent

    gpu_data = []
    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)

        try:
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        except pynvml.NVMLError as e:
            info = None

        try:
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
        except pynvml.NVMLError as e:
            utilization = None

        if info and utilization:
            gpu_data.append(
                {
                    "load": utilization.gpu,
                    "memoryUtil": (info.used / info.total) * 100,
                }
            )
        else:
            gpu_data = []

    except pynvml.NVMLError as e:
        gpu_data = []
    except Exception as e:
        gpu_data = []
    finally:
        try:
            pynvml.nvmlShutdown()
            logging.debug("NVML successfully shut down.")
        except Exception as e:
            logging.error(f"Error while shutting NVML down: {e}")
    return {"cpu": cpu_percent, "ram": ram_percent, "gpu": gpu_data}


# send resouce data to the frontend
def resource_monitor():
    while True:
        time.sleep(1)
        try:
            resources = get_system_resources()
            eel.update_system_resources(resources)()
        except Exception as e:
            logging.error(f"Error in resource_monitor: {e}")


if __name__ == "__main__":
    resource_thread = threading.Thread(target=resource_monitor, daemon=True)
    resource_thread.start()
    logging.info("Initialize OllamaTor GUI...")
    if getattr(sys, "frozen", False):
        pyi_splash.close()
    try:
        eel.start("index.html", mode="chrome", size=(1000, 800))
    except Exception as e:
        logging.error(f"Error while starting in 'chrome' mode: {e}")
        try:
            eel.start("index.html", mode="edge", size=(1000, 800))
        except Exception as e:
            show_missing_runtime_error()
            logging.error(f"Error while starting in 'edge' mode: {e}")
