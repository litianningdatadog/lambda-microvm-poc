FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

RUN dnf install -y nodejs npm && dnf clean all

COPY package.json .
RUN npm install --omit=dev

COPY app.js .

EXPOSE 8080 9000

CMD ["node", "app.js"]
