FROM python:3.12-slim

WORKDIR /app
COPY workbuddy_api_checkin.py config.json README.md ./

ENV PYTHONUNBUFFERED=1
ENV WORKBUDDY_STATE_PATH=/data/playwright_state.json
VOLUME ["/data"]

CMD ["python", "workbuddy_api_checkin.py"]
