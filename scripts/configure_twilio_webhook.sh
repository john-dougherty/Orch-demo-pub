#!/usr/bin/env bash
# Point the Twilio phone number's voice webhook at our public URL.
# Requires TWILIO_* env vars (loaded from /opt/hermes/.env) and a PUBLIC_BASE_URL.
# Usage:  PUBLIC_BASE_URL=https://foo.ngrok.app bash configure_twilio_webhook.sh
set -euo pipefail

if [[ -f /opt/hermes/.env ]]; then
  set -a; source /opt/hermes/.env; set +a
fi

: "${TWILIO_ACCOUNT_SID:?required}"
: "${TWILIO_AUTH_TOKEN:?required}"
: "${TWILIO_PHONE_NUMBER:?required}"
: "${PUBLIC_BASE_URL:?required (e.g. https://xxx.ngrok.app)}"

# Resolve the phone number SID for the number we own.
PN_SID=$(curl -sS -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$TWILIO_PHONE_NUMBER")" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["incoming_phone_numbers"][0]["sid"])')

echo "Configuring $TWILIO_PHONE_NUMBER (sid=$PN_SID) → $PUBLIC_BASE_URL"

curl -sS -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" -X POST \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$PN_SID.json" \
  --data-urlencode "VoiceUrl=$PUBLIC_BASE_URL/webhook/twilio/voice" \
  --data-urlencode "VoiceMethod=POST" \
  --data-urlencode "StatusCallback=$PUBLIC_BASE_URL/webhook/twilio/status" \
  --data-urlencode "StatusCallbackMethod=POST" \
  | python3 <<'PY'
import json, sys
d = json.load(sys.stdin)
print("  voice_url      =", d.get("voice_url"))
print("  status_callback =", d.get("status_callback"))
PY
