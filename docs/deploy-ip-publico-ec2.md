# Publicação direta na AWS EC2 — IP estático, sem Nginx nem ngrok

**Endereço de apresentação:** `https://34.193.62.218`

A aplicação é servida diretamente pelo **Uvicorn/Python na própria EC2**: porta pública **443 → 8443 (contêiner gateway)**. O gateway é uma aplicação FastAPI restrita por allowlist, que encaminha somente as operações de navegador ao controlador **isolado** na rede Docker. Nenhum usuário precisa de token ou login.

**Não exponha o controlador `controller.main` diretamente à Internet**: ele possui rotas internas de registro de storages, ACKs e controle de réplicas. O gateway Python em `controller/public_gateway.py` substitui o proxy Nginx e mantém esses endpoints inacessíveis aos visitantes. O projeto privado (`dcomp-drive`, porta 8000 vinculada a localhost) não sofre alterações.

## 1. Antes do TLS: garantir conectividade da EC2

Confirme no Security Group do Elastic IP:
- TCP **80** de `0.0.0.0/0` — somente para desafio HTTP-01 do Certbot e renovações. Não precisa de processo escutando permanentemente.
- TCP **443** de `0.0.0.0/0` — HTTPS do próprio gateway Uvicorn.
- TCP **22** restrita ao seu endereço IP de administração.
- **Não abrir** TCP 8000, 18001, 5432 ou 9000.

O Elastic IP deve estar associado à EC2; a subnet pública precisa de Internet Gateway e rota `0.0.0.0/0`. Cheque também `sudo ufw status`.

Como o Nginx não será usado, desligue-o:
```bash
sudo systemctl disable --now nginx
sudo systemctl disable --now ngrok || true
sudo ss -lntp | grep -E ':(80|443|18001) ' || true
```

Se o acesso TCP 80 ainda estiver duvidoso, utilize **temporariamente** um servidor HTTP Python, sem Nginx:
```bash
sudo mkdir -p /var/www/html/.well-known/acme-challenge
echo "DComp-Drive OK" | sudo tee /var/www/html/.well-known/acme-challenge/check.txt
sudo python3 -m http.server 80 --bind 0.0.0.0 --directory /var/www/html
```
De outra rede, abra `http://34.193.62.218/.well-known/acme-challenge/check.txt`. Deve aparecer `DComp-Drive OK`. **Pare esse servidor com Ctrl+C ANTES do Certbot.** Se não abrir, corrija Security Group/EIP/subnet/UFW: emitir certificado sem conectividade não resolverá o problema.

## 2. Certificado HTTPS válido para o IP, com Certbot standalone

O Certbot já foi instalado no ambiente Python `/opt/certbot/` (versão >=5.4). Não é necessário Snap nem servidor HTTP permanente. O plugin `standalone` inicia temporariamente seu próprio servidor HTTP na porta 80 para validação.

Teste:
```bash
sudo /opt/certbot/bin/certbot certonly --dry-run \
  --standalone --preferred-profile shortlived \
  --ip-address 34.193.62.218
```
Se passar, emita o certificado **definitivo**, confiável (sem `--staging` e sem `--dry-run`):
```bash
sudo /opt/certbot/bin/certbot certonly \
  --standalone --preferred-profile shortlived \
  --ip-address 34.193.62.218
sudo /opt/certbot/bin/certbot certificates
```
Atenção: os certificados de IP são de curta duração (~6 dias). O certificado será referenciado por `/etc/letsencrypt/live/34.193.62.218/fullchain.pem` e `privkey.pem`. Confirme os caminhos na saída do Certbot, caso ele use algum sufixo.

## 3. Atualizar a branch e subir o gateway Uvicorn com TLS

```bash
cd ~/Dcomp-Drive-Public
git fetch origin feature/dashboard-requisitos-rf1-rf12
git switch --detach origin/feature/dashboard-requisitos-rf1-rf12
sudo docker build -t dcomp-public-new-test .
sudo docker run --rm --network none dcomp-public-new-test python -m pytest -q tests
```
**Não prossiga se os testes não passarem.**

```bash
sudo docker compose -p dcomp-public \
  -f docker-compose.public.yml --env-file .env.public config --quiet
sudo docker compose -p dcomp-public \
  -f docker-compose.public.yml --env-file .env.public up -d --build
sudo docker compose -p dcomp-public \
  -f docker-compose.public.yml --env-file .env.public ps
```

A stack pública tem PostgreSQL, controlador, seis storages (cinco inicialmente registrados e o sexto standby) e um gateway HTTPS. Só **443** é vinculado publicamente. O controlador continua em `127.0.0.1:18001`, sem exposição direta.

## 4. Conferência pública (rede externa à EC2)

```bash
curl -fsS https://34.193.62.218/demo/config
python3 scripts/verify_public_ip.py
```
Os endpoints `/nodes/register`, `/files/prepare`, `/docs` e os controles `/demo/nodes/*` devem responder 404 pelo IP público. O teste do navegador deve incluir upload, download com SHA-256, renomear, mover, mkdir/rmdir, visualizar N réplicas, corrupção, falha e reparação, sexto nó, drain, concorrência, evidência de AES-GCM, reinício isolado, métricas e logs.

## 5. Renovação automática (obrigatória)

O Certbot usa modo `standalone`, que escuta temporariamente na porta 80; deixe o 80 livre para as renovações. Instale o timer e o serviço:
```bash
sudo cp deploy/dcomp-public-cert-renew.service /etc/systemd/system/
sudo cp deploy/dcomp-public-cert-renew.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dcomp-public-cert-renew.timer
sudo systemctl list-timers --all | grep dcomp-public
sudo /opt/certbot/bin/certbot renew --dry-run
```
O serviço executa o `renew`; se um certificado for renovado, o hook reinicia `dcomp-public-gateway-1` para recarregá-lo. Como o timer foi instalado depois da emissão, **é necessário confirmar que o `renew --dry-run` consegue concluir a validação**.

## Segurança e limites

- Sem Nginx, **a restrição de rotas está em `controller/public_gateway.py`**, com limites de corpo e de taxa por IP. Não exponha ou publique o `controller.main`.
- Visitantes não precisam de login ou token. O segredo interno do storage e a chave AES ficam apenas em `.env.public` (arquivo ignorado por Git e Docker build).
- Banco e volumes isolados por `-p dcomp-public`; projeto anterior permanece independente.
- O teste de falha lógica tem restauração automática; o reinício de RF10 afeta exclusivamente o controlador da sandbox.
- Não afirme tolerância a perda do host EC2: os storages são contêineres no mesmo servidor.
- A proteção por taxa é de aplicação, adequada a uma demonstração acadêmica, **não equivale a proteção DDoS**.
- Não execute `down -v` em nenhuma stack com dados a preservar.
