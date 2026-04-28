FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    nodejs npm \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY package.json .
RUN npm install --omit=dev

COPY app.js .

EXPOSE 8080

CMD ["node", "app.js"]
