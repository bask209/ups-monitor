FROM docker.io/library/python:3.13-alpine
RUN apk add --no-cache nut tzdata \
 && mkdir -p /var/state/ups /app /data
COPY nut/ /etc/nut/
COPY nut/entrypoint.sh /usr/local/bin/nut-entrypoint
COPY app/ /app/
RUN chmod 0640 /etc/nut/*.conf /etc/nut/upsd.users; chmod 0755 /usr/local/bin/nut-entrypoint
WORKDIR /app
CMD ["python", "-u", "/app/app.py"]
