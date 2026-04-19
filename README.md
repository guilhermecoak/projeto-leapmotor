# projeto-leapmotor

API pra receber ponto de GPS do app [Overland](https://overland.p3k.app/) (iOS) e guardar em sqlite. Tem alguns endpoints de análise pra ver no mapa os trajetos e rankear qual caminho eu uso mais pra chegar num lugar.

Rodando em Docker no meu homelab, acesso por Tailscale. Foi feito pra uso pessoal mas pode servir de base pra quem quiser algo parecido.

Também tem alerta de radar que puxa da base pública do OpenStreetMap (funciona pra SP, RJ e MG que são os estados que eu rodo, mas dá pra adicionar qualquer um).

## Pra que?

Queria ver meus trajetos visualmente e descobrir qual caminho de casa pro trabalho era mais rápido de verdade, não a estimativa do Waze. Também queria receber alerta de radar sem depender de app externo.

Comprei um carro novo (Leapmotor C10) que ainda não tem Apple CarPlay nem Android Auto nativos, então resolvi montar essas automações pra suprir.

Sou iniciante em programação, então o código pode conter erros. Toda ajuda é bem vinda.

## Como funciona

```
iPhone (Overland) --> Tailscale --> API (Docker)
                                      |
                                      +--> SQLite (pontos)
                                      +--> SQLite (radares do OSM)
                                      +--> ntfy.sh (alerta de radar)
```

- Overland manda batches de pontos GPS via HTTP POST
- API guarda em sqlite
- Se o device tiver "modo carro" ligado, checa se tem radar perto e manda push via ntfy
- Modo carro é toggle via endpoint (pretendo automatizar com Shortcut iOS quando BT do carro conectar)

## Endpoints

Todos com `Authorization: Bearer <token>` (ou `?access_token=<token>`):

- `POST /overland` — receber batch do app Overland
- `GET /stats` — contagem de pontos, último recebido, etc
- `GET /points?since=7d` — lista crua de pontos
- `GET /analysis?since=7d` — detecta trajetos, agrupa rotas recorrentes
- `GET /map?show_radares=true` — mapa HTML com Leaflet
- `GET /radares/near?lat=X&lon=Y&radius=1000` — radar em raio
- `POST /radares/refresh?state=São Paulo` — popula via Overpass API
- `POST /mode/car?device_id=X&enabled=true` — liga alerta de radar

## Rodar

```bash
# copia .env.example pra .env e coloca um token
cp .env.example .env
echo "GPS_TOKEN=$(openssl rand -hex 32)" > .env

# sobe
docker compose up -d --build

# popula radares (pode demorar ~30s por estado)
curl -X POST -H "Authorization: Bearer $(grep GPS_TOKEN .env | cut -d= -f2)" \
  "http://localhost:8000/radares/refresh?state=São Paulo"
```

## Configurar o Overland no iPhone

Na tela de settings do app:
- **Receiver Endpoint URL:** `http://<seu-ip>:8000/overland`
- **Access Token:** o `GPS_TOKEN` do `.env`
- **Device ID:** qualquer string (ex: `iphone-seu-nome`)

Configs que funcionaram bem pra mim:
- Continuous Tracking Mode: `Both`
- Desired Accuracy: `10m`
- Activity Type: `Car`
- Min Distance Between Points: `10m`
- Min Time Between Points: `5s`
- WiFi Zone ativado com nome da rede de casa (pra não tracker quando estacionado em casa)

## Segurança

Eu rodo atrás de Tailscale, então nunca exponho publicamente. O token é pra evitar que qualquer device no Tailscale mande pontos — não substitui HTTPS se for expor na internet.

Se for colocar público, bota atrás de um reverse proxy com TLS (nginx + certbot, Caddy, etc) e dobra a atenção no token.

## Estrutura

- `app.py` — FastAPI + endpoints
- `analysis.py` — segmentação de trajetos, agrupamento de rotas, mapa Leaflet
- `radares.py` — scraper OSM + busca proximidade + ntfy
- `Dockerfile` + `docker-compose.yml` — deploy

## Licença

MIT. Vai em frente.
