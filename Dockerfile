FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY weather_analysis ./weather_analysis
COPY dashboard ./dashboard
COPY .streamlit ./.streamlit

# Editable: `weather-analysis dashboard` resolves dashboard/app.py relative to
# the package directory, so the source tree has to stay where it was installed.
RUN pip install -e .

EXPOSE 8501

CMD ["weather-analysis", "dashboard"]
