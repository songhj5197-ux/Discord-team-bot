# 무료 클라우드 호스팅용 (Koyeb·Cloudtype 등 Docker 배포)
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# 호스팅이 PORT 환경변수를 넣어 주면 bot.py 가 이 포트로 헬스체크 서버를 엽니다.
EXPOSE 8080

CMD ["python", "bot.py"]
