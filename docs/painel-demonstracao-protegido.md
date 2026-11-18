# Demonstração acadêmica pelo dashboard (modo protegido)

Esta opção permite realizar no navegador os quatro passos exigidos na Parte 1:
upload com N=3, download/SHA-256, falha lógica de storage com recuperação
automática e corrupção isolada de uma réplica descartável com scrubbing real.

## Segurança e pré-condições

- **NÃO habilite DEMO_MODE com a porta 8000 aberta à internet**, principalmente
  porque a API principal ainda não implementa autenticação de usuários.
- Restrinja a porta 8000 a 127.0.0.1 em docker-compose.yml; remova a regra TCP
  8000 do Security Group público. Acesse via túnel SSH.
- O token administrativo precisa ter no mínimo 32 caracteres e não deve ser
  enviado por HTTP público, registrado em logs nem adicionado ao Git.
- O modo de falha afeta um Storage Node inteiro e, portanto, temporariamente
  todos os objetos nele existentes. Faça testes só em **laboratório descartável**,
  com backups e sem dados de terceiros.
- Não há acesso ao socket Docker. A falha simulada é real do ponto de vista
  da disponibilidade HTTP e dos heartbeats, mas **não desliga o contêiner**.
- O protótipo usa cinco storages lógicos no mesmo host EC2: não prova
  tolerância à perda física da instância.

## Ativação na EC2 (somente laboratório isolado)

No Ubuntu, entre em \`~/Dcomp-Drive\` e confira a branch testada.

1. Defina \`DEMO_MODE=true\` no arquivo \`.env\` local.
2. Gere uma credencial longa, por exemplo:
   \`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'\`.
3. Coloque o resultado em \`DEMO_ADMIN_TOKEN=...\` no \`.env\`.
4. Garanta que o serviço \`controller\` exponha somente:
   \`127.0.0.1:8000:8000\` no Compose.
5. Inicie \`sudo docker compose up -d --build\` e rode
   \`sudo docker compose exec -T controller python -m pytest -q tests/test_web_dashboard.py\`.
6. Abra túnel privado no Windows:
   \`ssh -N -i "$HOME\Downloads\dcomp-drive.pem" -L 8000:127.0.0.1:8000 ubuntu@34.193.62.218\`.
7. No navegador, abra \`http://localhost:8000/\`.
8. O painel **Modo de demonstração** será exibido; informe o token no campo
   administrativo. Ele permanece somente na página, não em localStorage.

## Sequência na própria interface (até 5 minutos)

1. **Upload**: selecione um arquivo local descartável, destino
   \`/demo-replicacao.txt\`, envie, abra **Detalhes** e mostre três réplicas
   em nós distintos, hash SHA-256 e status HEALTHY.
2. **Download**: clique em **Baixar** e mostre a mensagem de integridade
   SHA-256. O navegador precisa estar em localhost ou HTTPS.
3. **Falha**: na seção de demonstração, selecione um dos nós presentes
   nas réplicas do arquivo. Clique em **Simular falha lógica**. Mostre
   status SUSPECT/DEAD e o novo destino de réplica no painel atualizado
   a cada dois segundos. Aguarde 3/3 e HEALTHY antes de **Restaurar nó**.
4. **Corrupção**: faça upload de um SEGUNDO arquivo descartável no destino
   \`/demo-corrupcao.txt\`. Com todos os nós novamente saudáveis, informe
   esse caminho no painel e clique em **Corromper 1 réplica e verificar**.
   Um byte de uma réplica física é alterado; o scrubbing SHA-256 roda e
   comunica a corrupção ao controlador. O reparador cria uma nova cópia.
   Mostre o evento registrado e as três réplicas recuperadas.
5. **Fechamento**: explique que a interface web transfere pelo gateway do
   controlador, enquanto a CLI oferece transferência direta aos storages.

## Critérios de prova e troubleshooting

- O registro do painel informa ações efetuadas, alterações de estado
  e identificadores detectados pelo scrubbing. Para logs persistentes,
  consulte \`sudo docker compose logs controller storage-1 ...\` depois.
- Se a recuperação for muito rápida para mostrar estados intermediários,
  destaque os eventos e a mudança na lista dos nós que hospedam réplicas.
- O relatório 3/3 só é conclusão suficiente quando as cópias aparecem em
  nós distintos depois da falha e da corrupção.
- Se um comando falhar, **não repita corrupção em arquivos reais**. Confirme
  5 storages HEALTHY, botão Restaurar e token correto antes de continuar.
- \`DEMO_MODE=false\` (ou token ausente) esconde o painel e retorna erro
  nas rotas administrativas. Não use \`docker compose down -v\`.

**Para entrega acadêmica**: registre que o botão "Simular falha lógica"
não encerra um contêiner Docker; ele interrompe respostas HTTP e heartbeats
do nó. Isso reproduz indisponibilidade de serviço para o detector de
falhas, mas não simula perda física de disco ou desligamento da EC2.
