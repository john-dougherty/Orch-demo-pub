import sys
from hermes.config import settings

def check():
    print("🔍 Validating environment...")
    required = [
        ("Ollama", settings.ollama_base_url),
        ("Twilio SID", settings.twilio_account_sid),
        ("QBO Client ID", settings.qbo_client_id),
    ]
    
    missing = []
    for label, val in required:
        status = "✅" if val and "XXX" not in val else "❌"
        print(f"{status} {label}")
        if status == "❌":
            missing.append(label)
            
    if missing:
        print("\n⚠️  Missing or masked credentials found. Check your .env file.")
        sys.exit(1)
    else:
        print("\n🚀 Environment looks ready for deployment!")

if __name__ == "__main__":
    check()
