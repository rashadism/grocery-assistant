FROM python:3.12-slim AS dcv-sdk
RUN apt-get update && apt-get install -y --no-install-recommends wget gnupg unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG DCV_SDK_VERSION=1.13.2-1074
WORKDIR /tmp/dcv
# Amazon DCV Web Client SDK's EULA forbids redistributing it, so fetch and verify it at build time
# instead of vendoring it: https://docs.aws.amazon.com/dcv/latest/websdkguide/getting-started.html
RUN wget -q https://d1uj6qtbmh3dt5.cloudfront.net/NICE-GPG-KEY \
    && gpg --import NICE-GPG-KEY \
    && wget -q https://d1uj6qtbmh3dt5.cloudfront.net/webclientsdk/nice-dcv-web-client-sdk-${DCV_SDK_VERSION}.zip \
    && wget -q https://d1uj6qtbmh3dt5.cloudfront.net/webclientsdk/nice-dcv-web-client-sdk-${DCV_SDK_VERSION}.zip.sign \
    && gpg --verify nice-dcv-web-client-sdk-${DCV_SDK_VERSION}.zip.sign nice-dcv-web-client-sdk-${DCV_SDK_VERSION}.zip \
    && unzip -q nice-dcv-web-client-sdk-${DCV_SDK_VERSION}.zip \
    && mv nice-dcv-web-client-sdk/dcvjs-umd /dcvjs

FROM python:3.12-slim
WORKDIR /app
COPY app.py index.html ./
COPY static ./static
COPY --from=dcv-sdk /dcvjs ./static/dcvjs
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8080
CMD ["python", "app.py"]
