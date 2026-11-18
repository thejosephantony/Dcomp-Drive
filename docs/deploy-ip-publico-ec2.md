# Publicar DComp-Drive no Elastic IP da EC2 — laboratório isolado

**Objetivo:** `https://34.193.62.218/`, sem login, senha, token ou localhost para o visitante; execução real dos ensaios RF1–RF12 pelo navegador. A EC2 continua hospedando o serviço. **O laboratório público é descartável e diferente do cluster privado existente.**

## 0. Pré-requisitos

- Elastic IP 34.193.62.218 **associado à instância atual**.
- Ubuntu 24.04, Docker Compose e chave SSH de administração.
- Security Group: liberar TCP **80 e 443** para visitantes; **22 apenas ao IP do administrador**. **Não abrir 8000, 18001, 9000 nem 5432**.
- Parar túnel ngrok se não for mais necessário: `sudo systemctl disable --now ngrok`.
- RAM, disco e orçamento para segunda stack de 8 contêineres (PostgreSQL, controller, seis storages). Instância pequena exige revisão: usar `df -h /`, `free -h`, `sudo docker system df`. Com disco raiz pequeno, ampliar EBS antes de prosseguir. Não limpar volumes.
- Testes automatizados da branch aprovados; não confundir falhas de runner GitHub com erros de teste.

## 1. Teste de código sem alterar o cluster privado

```bash
cd ~/Dcomp-Drive
git fetch origin feature/dashboard-requisitos-rf1-rf12
git worktree add ../Dcomp-Drive-Public origin/feature/dashboard-requisitos-rf1-rf12
cd ~/Dcomp-Drive-Public
sudo docker build -t dcomp-public-rf-test .
sudo docker run --rm --network none dcomp-public-rf-test python -m pytest -q tests
```

Se houver qualquer falha, **parar antes do deploy**, corrigir na PR e repetir.

## 2. Subir um laboratório descartável com volumes e banco próprios

```bash
cd ~/Dcomp-Drive-Public
python3 scripts/init_public_lab.py
sudo docker compose -p dcomp-public -f docker-compose.public.yml --env-file .env.public config --quiet
sudo docker compose -p dcomp-public -f docker-compose.public.yml --env-file .env.public up -d --build
sudo docker compose -p dcomp-public -f docker-compose.public.yml --env-file .env.public ps
curl -fsS http://127.0.0.1:18001/demo/config
```

Esperado: `{"enabled":true,"public":true}`. O `controller` fica em **127.0.0.1:18001**, não exposto à internet. O cluster anterior segue em **127.0.0.1:8000** com seus volumes intocados. A stack pública começa com cinco nós registrados; sexto está em espera para RF6. **Nunca executar `down -v` no projeto privado.**

O script gera automaticamente um token **interno** (apenas controlador → storage), senha PostgreSQL e chave AES-256-GCM. Não exibir esses valores, não copiar o `.env.public` para Git e não recriar a configuração depois de gravar dados cifrados.

## 3. HTTPS válido sobre IP, sem domínio e sem ngrok

Let's Encrypt oferece certificados de IP público de aproximadamente seis dias. **Certbot >= 5.4** suporta IP no modo webroot (ver https://letsencrypt.org/2026/03/11/shorter-certs-certbot/). Renovação automática é obrigatória.

No host Ubuntu, instale e habilite Nginx:

```bash
sudo apt update
sudo apt install -y nginx snapd
sudo systemctl enable --now nginx
sudo mkdir -p /var/www/html/.well-known/acme-challenge
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo /snap/bin/certbot --version
```

Confirme que a versão é 5.4 ou superior e que a porta pública 80 responde antes de solicitar o certificado. O domínio nginx padrão deve servir arquivos em `/var/www/html`.

```bash
sudo /snap/bin/certbot certonly --webroot -w /var/www/html \
  --preferred-profile shortlived --ip-address 34.193.62.218
```

Siga as instruções interativas do Certbot. **Se a emissão falhar, não publicar HTTP sem TLS.**

Quando o certificado existir em `/etc/letsencrypt/live/34.193.62.218/`, instale a configuração com a allowlist de endpoints públicos:

```bash
sudo cp deploy/nginx-dcomp-public.conf /etc/nginx/sites-available/dcomp-drive-public
sudo ln -sf /etc/nginx/sites-available/dcomp-drive-public /etc/nginx/sites-enabled/dcomp-drive-public
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Para renovar automaticamente e recarregar o nginx:

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\n/usr/bin/systemctl reload nginx\n' | \
  sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo /snap/bin/certbot renew --dry-run
```

Verificar que existe temporizador do Certbot instalado (`systemctl list-timers | grep -i certbot`) e que ele renova certificados de seis dias com antecedência suficiente. Não considerar pronto sem `renew --dry-run` e teste externo.

## 4. Testes públicos (navegador anônimo, rede fora da EC2)

```bash
curl -fsSI https://34.193.62.218/
curl -fsS https://34.193.62.218/demo/config
curl -i https://34.193.62.218/nodes/register
curl -i https://34.193.62.218/demo/nodes/storage-1/offline
```

Os dois últimos devem retornar **404/403**, sem permitir operações administrativas internas. Abra `https://34.193.62.218/` em **outro celular/rede**, sem login e sem aviso de certificado. Acesse RF1, faça upload de arquivo descartável `/demo-arquivo.txt`, teste rename/move/ls/mkdir/rmdir, N=3, hash e cópias; em seguida RF4, RF5, RF6, RF7, RF8, RF9, RF10, RF11, RF12.

## 5. Limites e segurança

- O usuário público não vê `DEMO_ADMIN_TOKEN`. Endpoints do controlador principal não são expostos pelo Nginx.
- O IP publica **somente** o ambiente `dcomp-public`; PostgreSQL, volumes, rede Docker e chave de criptografia são independentes do projeto `dcomp-drive`.
- Limites: 1 MiB por upload, 100 arquivos publicados, 32 MiB por storage, 6 req/s por IP com burst, 1 teste de restart por 3 minutos (além de limite de IP no Nginx).
- Falha lógica de um storage se restaura automaticamente após cerca de 60 segundos; interrupções de navegador não deixam nó parado indefinidamente.
- Os seis storages são **contêineres numa única EC2**: demonstram falha lógica e re-replicação, não tolerância à perda do host físico.
- As URLs de backend interno não podem ser alteradas pelo visitante. Não expor socket Docker, /docs, /files/prepare ou /nodes/register.
- Não garantir resistência a tráfego adversarial elevado: um laboratório anônimo em IP público precisa de monitoramento de disco/CPU, custos, proteção DDoS externa se ficar permanentemente aberto e eventual limpeza programada **apenas** dos volumes públicos.

## 6. Estratégia para main e rollback

Esta branch deve ser mesclada na `main` **somente depois** de teste automatizado, validação Docker/Nginx e verificação em navegador. Deploy não se faz pelo IP: o código é puxado via Git na EC2, imagens recompiladas e contêineres da stack pública atualizados.

Se a stack pública falhar, interrompa apenas ela:

```bash
cd ~/Dcomp-Drive-Public
sudo docker compose -p dcomp-public -f docker-compose.public.yml --env-file .env.public stop
```

A stack privada continua intacta. Nunca usar `docker compose down -v` sem identificar o projeto/volumes.
