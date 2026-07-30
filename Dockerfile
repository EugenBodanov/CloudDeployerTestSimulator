FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SIMULATOR_MOCK_AWS=true

WORKDIR /app

COPY requirements.txt ./requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY main.py ./main.py
COPY simulator ./simulator

RUN useradd --create-home --shell /usr/sbin/nologin simulator \
    && chown -R simulator:simulator /app

USER simulator

EXPOSE 5000

CMD ["python", "main.py"]
