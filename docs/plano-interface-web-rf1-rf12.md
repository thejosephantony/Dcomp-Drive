# Plano da interface web — Cobertura RF1 a RF12 e laboratório público

**Fonte dos requisitos:** enunciado da disciplina COMP0470 — Sistemas Distribuídos, atividade DComp Drive, Parte 1. O painel deve permitir avaliação pelo navegador. **Exibir dados não significa comprovar a execução de um ensaio**: o avaliador deve conseguir observar resultados reais.

## Premissas de segurança para acesso público sem login

1. **Não expor o cluster existente** com os arquivos já gravados (incluindo PDFs acadêmicos) à internet. Não ativar controles de falha/corrupção/desligamento em ambiente compartilhado com dados a preservar.
2. Disponibilizar um **cluster de laboratório descartável com PostgreSQL e volumes próprios**, ou limpar/realocar os dados existentes antes de converter o ambiente em laboratório. Com a instância EC2 pequena, validar CPU, RAM, disco e custo antes de usar dois clusters em paralelo.
3. Publicar HTTPS no Elastic IP via proxy reverso; manter controlador e storages internos. Não abrir HTTP público na porta 8000. Não depender de ngrok ou de token administrativo do visitante.
4. Aplicar limites de tamanho e total de arquivos, taxa de requisições, concorrência, duração das ações, TTL/limpeza agendada e proteção contra abuso. Identificar cada sessão de demonstração e impedir que um usuário afete outra sessão quando possível.
5. **Falhas reais continuam isoladas ao cluster de demonstração**. Nunca encaminhar operações públicas para o socket Docker ou para endpoints administrativos da infraestrutura principal. Um controlador de ensaio pode operar apenas serviços e objetos predefinidos, com autorização interna.
6. Preservar as rotas administrativas atuais protegidas por token até concluir a separação. Não trocar `DEMO_MODE=true` por autorização universal no cluster atual.
7. Não publicar chaves AES, senhas PostgreSQL, arquivos `.env`, metadados pessoais ou logs completos contendo dados de usuários.

## RF1–RF12 — cobertura e critérios de aceitação

| Requisito | Interface alvo | Base existente | Pendente para aceitação |
|---|---|---|---|
| RF1 | Upload/download, lista de diretórios, criar/remover pasta, mover/renomear/excluir arquivo, metadados com hash/tamanho/data/cópias | `/files/content`, `/files/entries`, `/files/directories`, `/files/move`, `/files/entry`, `/files/metadata` | Validação integrada dos controles web |
| RF2 | Todas as interações de usuário no painel e API acessível | FastAPI, HTML/JS | Usabilidade, acessibilidade e mensagens |
| RF3 | Seletor de N, 1–5, erros 507 e localização das N réplicas em máquinas lógicas distintas | `/files/prepare`; gateway com parâmetro N na branch | Ensaio N=2,3,4,5 e excesso 507 |
| RF4 | SHA-256, download íntegro, injeção de corrupção **apenas em arquivo descartável**, scrubbing e 2/3 → 3/3 | `/demo/files/corrupt`, `/scrub` e monitor | Ensaio real no cluster público isolado |
| RF5 | Queda/restauração de storage, heartbeat, indicação de SUSPECT/DEAD, novo destino das réplicas, tempo até HEALTHY | Rotas de demo com token na implementação atual | Substituir por capacidade restrita ao laboratório público, sem atingir dados privados |
| RF6 | Lista de nós detectados com seus registros e reconciliação; ensaio com **novo sexto nó** sem reiniciar controller | `/nodes/register`, `/nodes/reconcile` | Integrar orquestração limitada para iniciar/ativar sexto nó; provar de ponta a ponta |
| RF7 | Capacidade por disco, percentuais de ocupação, drenagem controlada + liberação | `/status`, `/nodes/{id}/drain`, `/undrain` | Expor somente na sandbox, com guardas de integridade |
| RF8 | Duas escritas simultâneas de conteúdo descartável, resultados HTTP, leitura final com hash correto | Protocolo de escrita pendente e conflito 409 | Criar teste UI com relatório de respostas e leitura final |
| RF9 | Mostrar se AES-256-GCM está ligado, cifragem real no storage e hash de plaintext; nunca mostrar chave | `storage.crypto`, metadados físicos | Endpoint limitado para evidência de ciphertext, sem configuração sensível pública |
| RF10 | Snapshot do catálogo, reinicialização controlada do controller da sandbox, comparação de hash/versões após retorno | PostgreSQL persistente, provas prévias | Instrumentar ensaio de reinício sem disponibilizar reinício de infraestrutura principal |
| RF11 | Dashboard de nós/replicação/capacidade e reparações em andamento | `/status`, `/metrics`, `/files/catalog` | Validar métricas e tempo de atualização |
| RF12 | Timeline estruturada, timestamp/componente/evento/identificador e filtros sem segredos | Logs JSON em stdout | Expor somente eventos sanitizados e com retenção limitada |

## Entrega progressiva

### Fase A — Operações de uso comum
- RF1 completo na interface com rotas existentes.
- RF3 com seletor N no gateway e feedback claro quando não existem destinos.
- RF7 e RF11 com painel de capacidade e estado, sem controles destrutivos abertos.
- Matriz de rastreabilidade que identifica os ensaios ainda pendentes.

### Fase B — Laboratório público descartável
- Stack/volumes/metadata isolados, proxy HTTPS no Elastic IP, taxas e cotas.
- Recuperação automática de um nó com botão sem token **somente** nesse ambiente.
- Scrubbing e corrupção restritos a objetos da sessão de demonstração.
- Políticas de restauração automática e watchdog após interrupções.

### Fase C — Evidências dos demais requisitos
- RF6: sexto nó, registro e reconciliação.
- RF7: drain seguro com movimentação e recomposição.
- RF8: concorrência real com resultado das duas gravações e verificação final.
- RF9: comprovação de cifragem física sem divulgar chave.
- RF10: ensaio de reinício restrito à sandbox e comparação do catálogo.
- RF12: eventos estruturados, timestamp e filtros, sem segredos.

## Critérios para disponibilizar ao professor

- Todos os testes unitários e integração da branch aprovados.
- Ensaios reais RF1–RF12 documentados com resultados observáveis na interface; itens não executados identificados como pendentes.
- `https://<elastic-ip>` acessível em janela anônima e celular sem login, com certificado válido.
- Todas as ações públicas afetando somente dados e recursos de teste; dados existentes preservados.
- Cluster funcional após falha, corrupção, drain e reinicialização de teste.
- Sem rota pública que altere os storages privados, PostgreSQL principal, Docker socket ou chaves de criptografia.

## Uso do gerenciador RF1 (nova interface, aguardando testes)

1. **PUT**: escolher arquivo e destino absoluto na seção Arquivos, escolher N, enviar.
2. **GET**: usar botão Baixar da tabela; SHA-256 deve ser verificado após download em HTTPS ou localhost.
3. **Renomear**: botão Renomear, informar apenas o nome novo; mesmo diretório. Internamente usa `POST /files/move`.
4. **Mover**: botão Mover, informar caminho absoluto completo de destino, com pasta de destino previamente criada; mantém hash e versão física.
5. **Remover arquivo**: botão Excluir e confirmação; saída indica se limpeza física ficou pendente.
6. **Listar diretório**: aba Diretórios, botão Abrir diretório; botão Voltar uma pasta ou Abrir na linha.
7. **Criar diretório**: abrir diretório pai, informar nome da nova pasta, botão Criar pasta aqui.
8. **Remover diretório**: botão Remover pasta atual ou Excluir pasta da linha; somente vazio, não raiz.
9. **Metadados**: botão Detalhes mostra bytes, data, SHA-256, versão, fator N, réplicas e estado.
10. **Estados**: HEALTHY, UNDER_REPLICATED, RECOVERING e UNAVAILABLE são reportados pelo controlador, não simulados na UI.

**Nota:** renomear/mover pastas inteiras não é suportado no endpoint de namespace atual; somente arquivos são movidos. Testar a interpretação do requisito com a equipe da disciplina e estender com segurança se necessário. Para a apresentação, o RF1 de arquivo é satisfazível pela UI após teste integrado.

**Não declarar o RF1 aprovado antes do teste de navegador em EC2.** Os testes CI podem falhar sem executar runner: distinguir infraestrutura CI de falha no código.
