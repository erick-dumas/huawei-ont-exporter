FROM python:3.14-slim

ENV ONT_IP="127.0.0.1" \
    ONT_USERNAME="CHANGE_ME" \
    ONT_PASSWORD="CHANGE_ME" \
    ONT_PASSWORD_ENCODING="base64" \
    ONT_LANGUAGE="english" \
    # Variables de tiempo
    REQUEST_TIMEOUT_SECONDS="10" \
    POLL_INTERVAL_SECONDS="120" \
    MAX_BACKOFF_SECONDS="120" \
    # Variables de ajecucion
    APP_HOST="0.0.0.0" \ 
    APP_PORT="8000" \
    LOG_LEVEL="INFO"

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY . /code

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]