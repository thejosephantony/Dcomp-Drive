# RF4 — Demonstração segura de corrupção e recuperação automática

Este procedimento é exclusivo do laboratório DComp Drive. Ele cria **um novo arquivo de teste**, corrompe **somente uma réplica** e confirma que o sistema mantém leituras íntegras e recompõe o nível N=3.

## Pré-condições

1. Usar a branch `feature/rf4-auto-corruption-repair` e executar toda a suíte pytest com sucesso.
2. Verificar `sudo docker compose ps`, `curl -fsS http://127.0.0.1:8000/health` e que há pelo menos 4 Storage Nodes saudáveis para N=3.
3. Não executar `docker compose down -v`, não mexer no PostgreSQL e não corromper arquivos dos outros testes.
4. A porta 8000 permanece restrita a 127.0.0.1; os nós não possuem portas públicas.

## Passos: todos no Ubuntu da EC2, dentro de ~/Dcomp-Drive

### 1. Gravar um arquivo exclusivo para o teste

```bash
printf 'DComp RF4: teste controlado de corrupcao e auto-reparo.\n' > /tmp/rf4-corrupcao.txt
sha256sum /tmp/rf4-corrupcao.txt
curl -fsS -X PUT --data-binary @/tmp/rf4-corrupcao.txt \
  'http://127.0.0.1:8000/files/content?path=%2Frf4-corrupcao.txt'
```

A resposta deve apresentar `status: HEALTHY` e três réplicas.

### 2. Descobrir onde está a primeira réplica e seu ID físico

```bash
NODE=$(curl -fsS 'http://127.0.0.1:8000/files/metadata?path=%2Frf4-corrupcao.txt' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["replicas"][0]["node_id"])')

WRITE_ID=$(sudo docker compose exec -T controller python -c \
  "from sqlalchemy import select; from controller.database import SessionLocal; from controller.file_models import FileEntry; s=SessionLocal(); print(s.scalar(select(FileEntry.current_version_id).where(FileEntry.path=='/rf4-corrupcao.txt')) or ''); s.close()")

echo "Nó: $NODE"
echo "ID da versão: $WRITE_ID"
```

**Confira os valores antes da próxima etapa.** `NODE` deve ser um dos cinco storages e `WRITE_ID` deve ser um identificador hexadecimal de 32 caracteres. Caso contrário, interrompa a demonstração.

### 3. Corromper intencionalmente UMA réplica desse arquivo de teste

```bash
sudo docker compose exec -T "$NODE" python -c \
  "from storage.main import store; store.corrupt_for_testing('$WRITE_ID'); print('Copia de teste alterada')"
```

O comando acima altera um byte de **apenas um objeto** e não habilita o endpoint HTTP de injeção de falhas.

### 4. Executar scrubbing manualmente para acelerar a demonstração

```bash
sudo docker compose exec -T controller python -c \
  "import httpx; r=httpx.post('http://$NODE:9000/scrub', timeout=30); print(r.status_code); print(r.json())"
```

Esperado: `corrupted_objects: 1`, com `WRITE_ID` em `corrupted_ids`. O Storage Node reporta a corrupção ao controlador. O controlador valida o metadado físico, marca a réplica como CORRUPTED e o reparador cria uma nova cópia em um nó disponível. A cópia defeituosa só é removida depois do N=3 reestabelecido.

O scrubbing também roda automaticamente a cada 15 segundos (configurável via `SCRUB_INTERVAL_SECONDS`).

### 5. Verificar reparação, logs e hash original

```bash
sleep 5
curl -fsS 'http://127.0.0.1:8000/files/metadata?path=%2Frf4-corrupcao.txt'
curl -fsS 'http://127.0.0.1:8000/files/content?path=%2Frf4-corrupcao.txt' \
  -o /tmp/rf4-recuperado.txt
sha256sum /tmp/rf4-corrupcao.txt /tmp/rf4-recuperado.txt

sudo docker compose logs --since=2m controller "$NODE" \
  | grep -E 'scrub_corruption_detected|replica_corruption_confirmed|repair_completed|corrupted_replica_discarded'
```

Esperado: `status HEALTHY`, 3/3 réplicas e **hashes SHA-256 iguais**. Caso o reparo não apareça em 5 segundos, aguarde mais alguns ciclos e examine os logs; não corrompa outra réplica.

## Segurança / limitações

- O teste prova a falha de uma **réplica**. Não demonstra tolerância à perda da EC2 inteira, pois os cinco containers compartilham o mesmo host EBS.
- O controlador e os nós não implementam autenticação; mantenha a porta 8000 no loopback e acesse por túnel SSH.
- O controlador confirma corrupção usando metadado fornecido pelo Storage Node, não atestação criptográfica de origem; a confiança se restringe à rede interna de laboratório.
- O algoritmo opera sobre objetos completos (limite do gateway: 32 MiB, por padrão) e não sobre chunks.
- Para coleta de evidências, registre hora, stdout do scrubbing, `/files/metadata`, logs de eventos, hash original e recuperado.
