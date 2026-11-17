# Parte 1 — Complementos RF1, RF5, RF7, RF11, RF12

Este PR foi preparado para validação pela equipe. Os 52 testes da versão anterior passaram na AWS, mas **as mudanças deste PR precisam de nova execução de testes e de demonstração de integração**.

## RF1 — Metadados
- Exibir data de criação da versão no `stat`.
- Retornar estado `RECOVERING` durante re-replicação, quando existe ao menos uma origem viva.

## RF5 — Ordem e limites de reparos
- Primeiro reparar versões com menos réplicas vivas.
- `MAX_CONCURRENT_RECOVERIES` limita tarefas de reparação simultâneas (padrão 1).
- `REPAIR_BANDWIDTH_BYTES_PER_SEC` limita a velocidade **de cada reparo** (0 = sem limite adicional). O limite não representa garantia de banda agregada entre vários reparos simultâneos.
- O reparador valida origem e destino com SHA-256 antes de publicar a cópia.

## RF7 — Drain
`POST /nodes/{id}/drain` marca `DRAINING`. Heartbeat e registro repetidos não desfazem a decisão. Novas gravações excluem o nó. O reparador pode utilizar as réplicas íntegras do nó drenando como fontes, copiá-las para outros nós e, **depois que N cópias saudáveis existirem**, descartar as antigas.

**Limites:** se houver menos de N outros nós saudáveis, a drenagem fica incompleta; aguarde adicionar um nó. Não desligue o storage antes da restauração de todas as réplicas. Após migrar as cópias, reabilite o nó via `POST /nodes/{id}/undrain`.

Exemplo (execute **somente depois da suíte passar**, em dados de laboratório, com 4 outros Storage Nodes saudáveis e capacidade suficiente):

```bash
curl -fsS -X POST http://127.0.0.1:8000/nodes/storage-1/drain
curl -fsS http://127.0.0.1:8000/status | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics | python3 -m json.tool
```

## RF11 — Estado do cluster
`GET /status` expõe nós, estados, capacidade e uso de cada disco, contagem de arquivos `HEALTHY`, `UNDER_REPLICATED`, `RECOVERING`, `UNAVAILABLE` e reparos ativos. `GET /metrics` acrescenta contagem de réplicas por estado, requisições, latência média e bytes/vazão média do gateway.

## RF12 — Registro estruturado
Os componentes usam JSON por linha com timestamp UTC, nível, componente, evento e campos extraídos de `chave=valor`. Defina `LOG_FORMAT=json` (padrão em código). Não registrar segredos nos valores.

## Validação sugerida no Ubuntu EC2

```bash
cd ~/Dcomp-Drive
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose exec -T controller python -m pytest -q tests
curl -fsS http://127.0.0.1:8000/status | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics | python3 -m json.tool
```

Não usar `docker compose down -v`. Depois dos testes automatizados, testar drenagem de nó com um arquivo descartável e verificar o SHA-256. Registrar limites do experimento no relatório.

## Ainda pendente de adequação à Parte 1

RF6 reconciliação dos nós que retornam; RF9 criptografia funcional com configuração; transferência direta cliente → Storage Nodes; completude do relatório (4–8 páginas), README, vídeo/apresentação e integração da stack de PRs.
