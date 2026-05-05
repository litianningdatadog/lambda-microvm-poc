FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    python3.11 \
    python3.11-pip \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY requirements.txt .
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080

CMD ["python3.11", "app.py"]
