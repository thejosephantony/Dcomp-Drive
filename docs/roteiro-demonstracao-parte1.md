# DComp Drive — roteiro da demonstração obrigatória (até 5 minutos)

**Objetivos exigidos pelo professor:** (1) upload com N réplicas, (2) leitura, (3) queda de nó e recuperação automática, (4) detecção de uma cópia corrompida. O dashboard web é bônus. Gravar a demonstração completa ou combinar apresentação presencial.

## Pré-requisitos

1. Todos os testes automatizados da branch integrada aprovados. Conferir `sudo docker compose ps` e `curl -fsS http://127.0.0.1:8000/health`.
2. Nunca usar `docker compose down -v`. Usar **somente arquivos criados para a demonstração**.
3. Cinco storages saudáveis antes do ensaio de falha; não derrubar o PostgreSQL nem o controller.
4. Preparar duas abas do terminal: uma para CLI e outra para acompanhar logs. Evitar publicar tokens, chave de criptografia, `.env` ou dados pessoais no vídeo.
5. Reservar um arquivo descartável exclusivo para corrupção e verificar se há backup do PostgreSQL e volumes. Não interromper a única réplica íntegra nem derrubar dois nós ao mesmo tempo.
6. O arquivo de demonstração deve ter um caminho novo: se `/video-parte1.txt` já existir de um ensaio anterior, escolher outro nome e atualizá-lo em todos os comandos.

## Cronograma sugerido

| Intervalo | Conteúdo | Evidência na tela |
| --- | --- | --- |
| 0:00–0:35 | Arquitetura e estado de cinco nós | Diagrama, Docker Compose e /status |
| 0:35–1:35 | Upload N=3 usando a CLI direta, `stat`, `ls` | Três Storage Nodes distintos |
| 1:35–2:05 | Download e SHA-256 | Digest original e recuperado iguais |
| 2:05–3:30 | Parar um Storage Node; reparo automático | DEAD, reparação, retorno a HEALTHY/3 |
| 3:30–4:40 | Corromper uma réplica descartável; scrub e reparação | Eventos scrub_corruption_detected, repair_completed |
| 4:40–5:00 | Conclusão | Hash preservado, breve menção às limitações |

## Comandos de preparação (fora dos cinco minutos)

No Ubuntu da EC2:

```bash
cd ~/Dcomp-Drive
mkdir -p demo-files
printf 'DComp Drive: demonstracao da Parte 1.\n' > demo-files/video-parte1.txt
sha256sum demo-files/video-parte1.txt
sudo docker compose ps
sudo docker compose exec -T controller python -m pytest -q tests
```

## Upload e leitura DIRETOS

```bash
sudo docker compose run --rm client put /work/video-parte1.txt /video-parte1.txt
sudo docker compose run --rm client stat /video-parte1.txt
sudo docker compose run --rm client ls /
sudo docker compose run --rm client get /video-parte1.txt /work/video-recuperado.txt
sudo sha256sum demo-files/video-parte1.txt demo-files/video-recuperado.txt
sudo cmp demo-files/video-parte1.txt demo-files/video-recuperado.txt
```

A CLI direta usa `/files/prepare` e faz os PUT/GET diretamente nos Storage Nodes. O controlador não recebe bytes do arquivo nesse fluxo.

## Falha de nó

Escolher **um Storage Node que conste nas réplicas do arquivo de demonstração** e interromper somente esse contêiner:

```bash
sudo docker compose stop storage-1  # SOMENTE se storage-1 estiver nas réplicas do arquivo
sleep 20
curl -fsS 'http://127.0.0.1:8000/files/metadata?path=%2Fvideo-parte1.txt'
sudo docker compose logs --since=3m controller | grep -E 'node_state_changed|repair_claimed|repair_completed'
curl -fsS 'http://127.0.0.1:8000/files/metadata?path=%2Fvideo-parte1.txt'
sudo docker compose start storage-1
# Conferir depois de retornar:
sudo docker compose run --rm client status
```

Aguardar o monitor detectar a queda e o reparador recuperar N=3. Se o nó não aparecer nas réplicas, não interrompê-lo; escolher um nó que efetivamente armazena o arquivo.

## Corrupção isolada e segura

Preparar **um segundo arquivo novo**. Confirmar seu ID e em qual Storage Node está armazenado antes de modificar um byte.

```bash
printf 'Arquivo exclusivo para demo de corrupcao.\n' > /tmp/video-corromper.txt
curl -fsS -X PUT --data-binary @/tmp/video-corromper.txt \
  'http://127.0.0.1:8000/files/content?path=%2Fvideo-corrupcao.txt'

curl -fsS 'http://127.0.0.1:8000/files/metadata?path=%2Fvideo-corrupcao.txt' | python3 -m json.tool
```

A resposta contém `version_id` e uma lista de `replicas[].node_id`. Antes de injetar corrupção, confirmar que as outras duas réplicas permanecem íntegras, que a chave de criptografia está preservada e que o objeto pertence exclusivamente ao arquivo descartável. **Substitua manualmente** `<VERSION_ID>` e `<STORAGE_CONFERIDO>` pelos valores obtidos **para este arquivo**, nunca por IDs de arquivos anteriores:

```bash
sudo docker compose exec -T <STORAGE_CONFERIDO> python -c \
  "from storage.main import store; store.corrupt_for_testing('<VERSION_ID>')"

# A rotina automática verifica a cada 15 segundos; se necessário, disparar:
sudo docker compose exec -T controller python -c \
  "import httpx; r=httpx.post('http://<STORAGE_CONFERIDO>:9000/scrub',timeout=30); print(r.json())"

sudo docker compose logs --since=4m controller <STORAGE_CONFERIDO> \
  | grep -E 'scrub_corruption_detected|replica_corruption_confirmed|repair_completed|corrupted_replica_discarded'

curl -fsS 'http://127.0.0.1:8000/files/metadata?path=%2Fvideo-corrupcao.txt'
curl -fsS 'http://127.0.0.1:8000/files/content?path=%2Fvideo-corrupcao.txt' \
  -o /tmp/video-corromper-baixado.txt
sha256sum /tmp/video-corromper.txt /tmp/video-corromper-baixado.txt
```

Se o scrubbing manual mostrar 0 objetos corrompidos, verificar os logs: o scrubbing automático pode ter identificado e reparado a corrupção primeiro, como aconteceu no teste anterior de 17/11. A captura deve mostrar um registro efetivo de corrupção e reparo, não apenas um status HEALTHY posterior.

## Narrativa sugerida para o vídeo

**0:00–0:35 —** “Nosso projeto usa um controlador FastAPI com metadados no PostgreSQL. Os cinco Storage Nodes compartilham uma EC2, mas mantêm volumes distintos. A CLI troca metadados com o controlador e transfere os bytes diretamente aos nós.”

**0:35–1:35 —** “Ao gravar o arquivo, o controlador reserva três destinos e confirma o arquivo somente após obter três cópias verificadas por SHA-256.” Mostrar stat com três storages distintos.

**1:35–2:05 —** “O arquivo baixado mantém exatamente o mesmo hash e o mesmo conteúdo binário do original.” Mostrar SHA-256 e cmp.

**2:05–3:30 —** “Interrompemos um nó que contém uma réplica. O monitor detecta a falha e o reparador copia o arquivo a outro nó, recompondo o fator N.” Mostrar status e logs antes de religar o storage.

**3:30–4:40 —** “Alteramos apenas a cópia de um arquivo descartável; o scrubbing detecta a corrupção, retira a réplica comprometida da contagem e restaura a cópia íntegra em outro nó.” Mostrar logs e novo hash.

**4:40–5:00 —** “A implementação foi validada com 64 testes e ensaios na AWS. Nossa principal limitação é que os nós lógicos estão na mesma EC2, portanto não demonstramos resiliência à falha do host físico.”

## Critérios de sucesso

- N=3 após upload, três nós/discos distintos.
- Download com SHA-256 igual ao original.
- Falha do nó registrada; novo destino escolhido automaticamente e estado volta a HEALTHY.
- Corrupção detectada, réplica defeituosa descartada e N=3 restaurado, sem afetar outros arquivos.
- **Nunca** afirmar tolerância à perda da EC2 física inteira; o ambiente possui cinco contêineres em um host.
