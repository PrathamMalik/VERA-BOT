FROM python:3.11-slim
WORKDIR /app
COPY bot.py ./
COPY vera ./vera
ENV PORT=8080 QUIET=1
EXPOSE 8080
CMD ["python", "bot.py"]
