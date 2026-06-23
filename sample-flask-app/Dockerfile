FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

RUN dnf install -y \
    python3.11 python3.11-pip \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file htop\
    && dnf clean all

COPY requirements.txt .
RUN pip3.11 install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 9000

CMD ["python3.11", "app.py"]
