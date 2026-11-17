# DComp Drive — Entrega da Parte 1 (18/11/2026)

**Universidade Federal de Sergipe, Sistemas Distribuídos (COMP0470).** Professor: **Rafael Oliveira Vasconcelos**. Integrantes: **Thayla Almeida Figueiredo**, **Joseph Antony dos Santos Leite** e **Jeferson Gonzaga dos Santos Filho**.

Este documento organiza os entregáveis do enunciado oficial. Não substitui o PDF acadêmico nem a demonstração prática.

## 1. Repositório, histórico e execução

Repositório: https://github.com/thejosephantony/Dcomp-Drive

A versão final deve estar na branch main, preservando os commits de cada integrante por merge commit (sem squash). Conferir autores e divisão de tarefas no histórico Git antes da submissão.

Em host com Docker Engine, Compose v2 e Git:

~~~bash
git clone https://github.com/thejosephantony/Dcomp-Drive.git
cd Dcomp-Drive
cp .env.example .env
# Definir uma senha privada em POSTGRES_PASSWORD no .env.
mkdir -p demo-files
docker compose config --quiet
docker compose up -d --build
# Aguardar as migrações e a API FastAPI inicializarem:
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:8000/health
docker compose ps
docker compose run --rm client status
~~~

Em Ubuntu sem acesso ao socket Docker, usar sudo. Não executar docker compose down -v se houver dados persistentes. Não expor portas de Storage Nodes, PostgreSQL ou a API sem autenticação à internet.

## 2. Comandos de verificação

~~~bash
docker compose exec -T controller python -m pytest -q tests
docker compose exec -T controller python scripts/test_concurrency_live.py
docker compose exec -T controller python scripts/test_rf3_allocation_live.py
curl -fsS http://127.0.0.1:8000/status | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics | python3 -m json.tool
~~~

Evidências coletadas na EC2 em **17/11/2026**: 64 testes automatizados aprovados, 1 aviso; RF8 com 12/12 leituras íntegras e duas escritas 201/409; RF3 N=2 e recusas 507 de N=6 e falta de capacidade; RF5 falha e recuperação; RF6 retorno e descarte de 5 cópias antigas; RF7 drain; RF9 criptografia AES-256-GCM verificada no disco; RF10 persistência; RF11 estado e métricas; RF12 logs estruturados. A corrupção e recuperação RF4 foram comprovadas anteriormente em arquivo descartável. Para detalhes, consultar docs/evidencias/parte1/.

Os cinco storages são **contêineres na mesma máquina física**; não há tolerância demonstrada à falha completa da EC2. A expansão para oito nós e mil arquivos pequenos é meta de ensaio, não capacidade já comprovada.

## 3. Relatório técnico — 8 páginas

**Arquivo PDF a anexar separadamente à submissão:** DComp_Drive_Relatorio_Tecnico_Parte1_Entrega.pdf

Inclui brasão da UFS, autores e professor com nomes completos, arquitetura e diagrama, protocolos REST/JSON, política de posicionamento, quórum W=N e decisão CAP, concorrência, algoritmos de integridade, criptografia, tolerância a falhas, limitações, evidências e divisão de tarefas.

**Conteúdo editável:** docs/relatorio-tecnico-parte1.md. O PDF precisa ser anexado pelo responsável à plataforma da disciplina; o Docker Compose não gera o relatório.

## 4. Demonstração obrigatória

É necessário **vídeo de até 5 minutos ou apresentação presencial**, comprovando todos os quatro passos: (1) gravar N réplicas; (2) ler e verificar SHA-256; (3) falha e re-replicação; (4) detecção de arquivo corrompido e reparação. Roteiro: docs/roteiro-demonstracao-parte1.md.

Não gravar em vídeo o conteúdo privado do .env, a chave ENCRYPTION_KEY_BASE64, a senha POSTGRES_PASSWORD ou dados pessoais. Injetar corrupção somente em arquivo novo, descartável e identificado.

## 5. Conferência final

- [ ] Código na main, sem perder commits dos três integrantes, CI revisada.
- [ ] README e Compose reproduzem controlador, PostgreSQL e cinco Storage Nodes.
- [ ] PDF de 8 páginas anexado e nomes completos conferidos; sem Maxwell.
- [ ] Vídeo de até 5 minutos gravado ou apresentação presencial acertada.
- [ ] Criptografia persistente: chave guardada com segurança **fora da EC2**.
- [ ] Nenhuma credencial, arquivo .env ou backup privado no repositório.
- [ ] Limitações e resultados documentados sem extrapolar as evidências.

**Parte 2:** avaliação quantitativa até 30/11/2026. Congelar a versão testada por tag Git no início da Parte 2, conforme o enunciado, e registrar qualquer correção posterior.
