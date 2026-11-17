# DComp Drive — roteiro da demonstração obrigatória (até 5 minutos)

**Objetivos exigidos pelo professor:** (1) upload com N réplicas, (2) leitura, (3) queda de nó e recuperação automática, (4) detecção de uma cópia corrompida. A interface web é um bônus; a gravação pode combinar navegador e terminal para mostrar os mecanismos reais. Gravar a demonstração completa ou combinar apresentação presencial.

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
| 0:00–0:35 | Abrir o dashboard web e mostrar arquitetura | Cinco Storage Nodes saudáveis, totais de arquivos/réplicas |
| 0:35–1:25 | Upload pelo navegador e detalhes do objeto | N=3, três nós distintos, SHA-256 e versão |
| 1:25–1:55 | Download pelo botão Baixar | SHA-256 conferido automaticamente pelo navegador |
| 1:55–3:15 | Parar um Storage Node pelo terminal e observar o reparo | Dashboard /status e logs; nova réplica e HEALTHY |
| 3:15–4:35 | Corromper réplica de arquivo descartável por terminal | Evento de scrub, reparação e íntegro no dashboard |
| 4:35–5:00 | Conclusão e demonstração de CLI direta (se couber) | Separação plano de controle/dados e limite de uma EC2 |

## Exibição da interface web (bônus)

O dashboard faz parte do próprio controlador FastAPI e é servido em `/`. Ele permite selecionar um arquivo, informar o caminho remoto, enviar, abrir **Detalhes**, consultar SHA-256 e localização das réplicas, baixar o objeto e acompanhar o estado dos cinco storages. A página atualiza os nós automaticamente a cada 10 segundos; o botão **Atualizar** antecipa a consulta.

**Windows PowerShell, terminal próprio para túnel SSH:**

~~~powershell
ssh -N -o ExitOnForwardFailure=yes -i "$HOME\Downloads\dcomp-drive.pem" -L 8000:127.0.0.1:8000 ubuntu@34.193.62.218
~~~

Abrir **http://127.0.0.1:8000/** no navegador. O caminho real da chave e o IP do host devem ser confirmados no ambiente antes da gravação. Não expor a porta 8000 no Security Group: a API ainda não tem autenticação. A transferência feita pelo navegador passa por `/files/content` no controlador; para provar a separação dos planos, mostrar também **uma** operação `docker compose run --rm client put/get` ou explicar que a CLI direta envia bytes aos Storage Nodes e que o dashboard é um gateway de conveniência.

**Sequência web sugerida:**
1. Mostrar cinco storages em HEALTHY.
2. Escolher um arquivo local descartável, definir caminho remoto novo (por exemplo `/video-web-20261118.txt`) e clicar em **Enviar para o cluster**.
3. Abrir **Detalhes** do arquivo e apresentar os três storages, discos, versão, tamanho e hash.
4. Clicar em **Baixar** e mostrar a verificação SHA-256.
5. Para a falha e a corrupção, alternar ao terminal para executar os comandos e voltar à página para mostrar a recuperação.

**Atenção:** não afirmar que o dashboard mede transferência direta cliente–storage. Nesse fluxo, os bytes passam pelo gateway do controlador, ao contrário da CLI Docker.

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

**0:00–0:35 —** “Esta é a interface web do DComp Drive. O projeto usa um controlador FastAPI com metadados no PostgreSQL. Os cinco storages compartilham uma EC2, mas mantêm volumes próprios. A interface mostra nós, arquivos e réplicas.”

**0:35–1:25 —** “Enviamos um arquivo pela interface. O controlador confirma a gravação depois que os três nós registram e verificam suas cópias.” Mostrar **Detalhes** com três storages distintos.

**1:25–1:55 —** “O download valida o SHA-256 e evita devolver conteúdo corrompido.” Mostrar botão **Baixar** e mensagem de integridade.

**1:55–3:15 —** “Interrompemos um nó que contém uma réplica. O monitor detecta a falha e o reparador copia o arquivo a outro nó, recompondo o fator N.” Mostrar status e logs antes de religar o storage.

**3:15–4:35 —** “Alteramos apenas a cópia de um arquivo descartável; o scrubbing detecta a corrupção, retira a réplica comprometida da contagem e restaura a cópia íntegra em outro nó.” Mostrar logs e novo hash.

**4:35–5:00 —** “A implementação foi validada com 64 testes e ensaios na AWS. Nossa principal limitação é que os nós lógicos estão na mesma EC2, portanto não demonstramos resiliência à falha do host físico.”

## Critérios de sucesso

- N=3 após upload, três nós/discos distintos.
- Download com SHA-256 igual ao original.
- Falha do nó registrada; novo destino escolhido automaticamente e estado volta a HEALTHY.
- Corrupção detectada, réplica defeituosa descartada e N=3 restaurado, sem afetar outros arquivos.
- **Nunca** afirmar tolerância à perda da EC2 física inteira; o ambiente possui cinco contêineres em um host.
