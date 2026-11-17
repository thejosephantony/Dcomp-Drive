# Interface web — DComp Drive

Painel de laboratório integrado ao controlador FastAPI. Sem Node.js, React ou dependências de frontend. O código fica em controller/web e é servido pela mesma origem da API.

## Funcionalidades reais

- Visão geral do cluster: GET /status com estados HEALTHY, SUSPECT e DEAD.
- Arquivos publicados: GET /files/catalog (até 200 entradas, ordenadas por caminho).
- Upload: PUT /files/content?path=/nome.ext, usando o gateway HTTP atual, com replicação configurada no controlador.
- Metadados e réplicas: GET /files/metadata?path=/nome.ext.
- Download: GET /files/content?path=/nome.ext, com verificação SHA-256 no navegador usando Web Crypto API.

O catálogo mostra a quantidade de réplicas saudáveis no instante da consulta; não promete que as cópias serão recuperadas durante a falha de toda a EC2.

## Publicação e acesso na AWS

O docker-compose.yml desta branch publica o controlador exclusivamente em 127.0.0.1:8000 na máquina EC2. Mantenha SSH/22 restrito ao seu IPv4 no Security Group. Não abra 8000, 5432 ou 9000 para a internet.

Na EC2, dentro do projeto:

    sudo docker compose up -d --build
    sudo docker compose ps
    curl http://127.0.0.1:8000/
    curl http://127.0.0.1:8000/files/catalog

No Windows, inicie o túnel em uma janela PowerShell e mantenha-o aberto:

    ssh -N -i "$HOME\Downloads\dcomp-drive.pem" -L 8000:127.0.0.1:8000 ubuntu@IPV4_PUBLICO_DA_EC2

Abra http://127.0.0.1:8000/ no navegador do Windows.

## Roteiro de demonstração (Parte 1)

1. Mostrar os cinco nós HEALTHY no painel.
2. Fazer upload de um arquivo pela interface e visualizar três réplicas em nós distintos.
3. Baixar o arquivo e confirmar a validação de SHA-256 pelo navegador.
4. Na EC2, parar um nó com cópia: sudo docker compose stop storage-1.
5. Observar o estado DEAD e a reconstrução em outro nó no painel.
6. Restaurar o nó: sudo docker compose start storage-1.
7. Demonstrar corrupção com o procedimento técnico separado, se implementado e validado.

## Limitações e segurança

- O controlador e a interface não possuem autenticação de usuários. Uso local ou por túnel SSH apenas; não publicar em endereço público sem autenticação, autorização e HTTPS.
- O gateway atual limita arquivos a 32 MiB por padrão (MAX_TRANSFER_BYTES) e recebe os bytes pelo controlador; não há tráfego direto navegador ↔ storage.
- Operações de diretórios (ls, mkdir, rmdir), remoção, renomeação/movimentação e drain não estão disponíveis no backend HTTP; o painel não exibe controles fictícios.
- Todos os cinco Storage Nodes compartilham uma única EC2 e um mesmo disco EBS subjacente, embora utilizem volumes Docker separados.
- Scrubbing automático não está ativado no docker-compose.yml atual (SCRUB_INTERVAL_SECONDS=0 por padrão); a demonstração de corrupção deve ser planejada e testada separadamente.
- Este painel não oferece métricas históricas, gráficos de latência/vazão ou autenticação.
- A lista retorna no máximo 200 arquivos; não existe paginação.
- É uma interface de apresentação acadêmica, não uma interface pronta para produção.
