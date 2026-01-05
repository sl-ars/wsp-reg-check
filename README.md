## WSP Registration check

- Check every 60 seconds
- `/hc` - Health Check

1. Build
```bash
docker build -t wsp-reg-check .
```

2. Run
```bash
docker run -d --name wsp-reg-check --restart unless-stopped \
  -e KBTU_USERNAME="..." \
  -e KBTU_PASSWORD="..." \
  -e TELEGRAM_BOT_TOKEN="..." \
  -e TELEGRAM_CHAT_ID="..." \
  -v "$(pwd)/data:/data" \
  wsp-reg-check
```


