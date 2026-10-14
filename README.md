# victus-thermal

Controle térmico no estilo OMEN Gaming Hub para o **HP Victus 15-fb2xxx**
(placa **8C2F**, Ryzen 5 8645HS + RTX 4050) no Ubuntu 26.04 com Secure Boot.

## O problema

O driver `hp-wmi` do kernel só ativa o controle térmico da HP em placas que
estão na sua lista interna, e a **8C2F não está nela** (nem no kernel 7.0.14,
nem no mainline). Sem isso:

- o modo térmico da HP (Desempenho/Balanceado) nunca é ativado; o "Desempenho"
  do GNOME só mexe na CPU, através do `amd-pmf`;
- o controlador de ventoinhas fica na curva mais conservadora: num teste de
  carga, a CPU chegou a **100 °C com as ventoinhas paradas por 30 s**;
- o driver NVIDIA não consegue ler da BIOS a temperatura-alvo e o modo de
  energia (`failed to get target temp from SBIOS`);
- o único controle de ventoinha disponível é `pwm1_enable`, com dois estados:
  máximo ou automático.

## A solução

| Parte | O que faz |
|---|---|
| `driver/` | O `hp-wmi.c` **exato** do kernel Ubuntu 7.0.0-38, mais **uma** entrada na tabela: `8C2F` → `victus_s_thermal_params`. Isso libera os perfis térmicos da HP, a troca de cTGP/PPAB da GPU, a leitura de RPM e o controle manual de ventoinha. É instalado via DKMS, assinado com a sua chave MOK. |
| `victus_thermal/` | Daemon (`victus-thermald`) com uma curva de ventoinha por perfil, proteção de emergência, watchdog do systemd e restauração garantida do controle do firmware. Só usa a biblioteca padrão do Python. |
| `victusctl` | CLI sem root para ver o status, forçar um modo e trocar de perfil. |
| `gnome-extension/` | Indicador na barra superior do GNOME: temperatura, RPM, perfil e modo das ventoinhas. |
| `tools/catch-workqueue-warning.sh` | Identifica o driver que causa o `WARNING workqueue.c:2351` visto antes dos congelamentos. |
| `extras/rtw89-stability.conf` | Contorno opcional para os timeouts do Wi-Fi Realtek 8852BE. |

O patch não faz **nenhum acesso direto ao controlador embarcado (EC)**: para
essa placa o deslocamento do EC é desconhecido (`HP_EC_OFFSET_UNKNOWN`), então
o driver usa apenas métodos WMI que a própria BIOS implementa e valida. A
família `victus_s` da 8C2F também foi confirmada, de forma independente, pelo
projeto [omen-fan-control](https://github.com/arfelious/omen-fan-control)
(v2.1.0, testado no 15-fb2xxx com kernel 7.x).

## Instalação

Rode num **terminal normal**: a etapa da chave MOK pede uma senha.

```bash
cd ~/Projects/victus-thermal
sudo ./install.sh check        # confere placa, kernel e Secure Boot
sudo ./install.sh deps         # dkms e ferramentas de build
sudo ./install.sh mok          # cria a chave de assinatura e pede o cadastro
```

Se a etapa `mok` pedir o cadastro da chave: **reinicie**. Na tela azul
"MOK management", escolha *Enroll MOK* → *Continue* → *Yes*, digite a senha e
depois *Reboot*. Só é preciso fazer isso uma vez.

```bash
sudo ./install.sh driver-test     # carrega o driver só até o próximo reboot
```

O `driver-test` verifica sozinho a interface de ventoinhas, o `pwm1`, os
perfis térmicos e os erros do kernel. **Se algo falhar, ele volta ao driver
original automaticamente.** Se tudo passar, teste à vontade: troque o modo de
energia no GNOME e jogue um pouco. Depois:

```bash
sudo ./install.sh driver-install  # permanente (DKMS, recompila sozinho no 7.0.x)
sudo ./install.sh daemon          # serviço de curva de ventoinha
sudo ./install.sh extension       # extensão da barra do GNOME, para você
```

Ou tudo de uma vez, parando onde precisar reiniciar: `sudo ./install.sh all`.
Rodar o `all` de novo é seguro e é também a forma de **atualizar**: ele pula o
que já está instalado, atualiza o serviço e a extensão, e só substitui o
`config.toml` se você não o editou.

## Uso

O **perfil** é o próprio **modo de energia do GNOME** (Economia, Equilibrado,
Desempenho, no menu do canto superior direito). Com o driver corrigido, ele
ativa o modo térmico real da HP, como o OMEN fazia, e o serviço troca a curva
de ventoinha junto. Também dá para usar a extensão ou o `victusctl`:

```bash
victusctl status                 # temperaturas, RPM, decisão atual
victusctl profile performance    # low-power | balanced | performance
victusctl mode max               # curve | auto | max (volta a "curve" no reboot)
```

Para jogar: **carregador ligado + perfil `performance`**. Isso ativa o modo
térmico de desempenho da HP, o cTGP da GPU e a curva mais agressiva.

### Extensão do GNOME

Instalada pelo `sudo ./install.sh all` (ou `extension`). Depois **saia e
entre de novo na sessão** uma vez: no Wayland o GNOME só carrega extensões
novas ou atualizadas no login. Para instalar à mão, sem sudo:
`./gnome-extension/install.sh` (remover: `--uninstall`).

O ícone de ventoinha na barra mostra a temperatura do sensor mais quente:
laranja a partir de 80 °C, vermelho a partir de 90 °C ou em emergência. O
menu mostra CPU/GPU/iGPU, RPM, tomada/bateria e o que o daemon está fazendo,
e permite trocar o perfil (Economia, Equilibrado, Desempenho) e o modo das
ventoinhas (Automático (recomendado), Somente firmware HP, Máximo). A extensão não tem
privilégios: lê `/run/victus-thermal/status.json` e chama o `victusctl`.

### Curvas

Edite `/etc/victus-thermal/config.toml`; a mudança vale em até 2 s. Os
pontos são `[temperatura °C, ventoinha %]` e a entrada é o sensor mais quente
(CPU, iGPU ou GPU NVIDIA). `0 %` deixa o firmware HP decidir e `100 %` usa o
modo MAX.

As curvas padrão começam em 0 %: em carga leve **o firmware HP manda**, no
modo térmico do perfil escolhido, e a curva só assume quando esquenta de
verdade, onde o firmware é tímido demais. Ao assumir, o serviço **nunca
começa mais devagar do que o firmware estava girando**. Um arquivo inválido é
recusado por inteiro (o motivo vai para `journalctl -u victus-thermal`) e
os padrões continuam valendo.

### Segurança

- **Emergência:** qualquer sensor ≥ 95 °C força MAX, acima de qualquer outra
  configuração, até todos voltarem a ≤ 85 °C.
- **Sensor de CPU ilegível:** o controle volta ao firmware, nunca chuta.
- **Daemon parou, travou ou caiu:** o firmware reassume. O próprio daemon faz
  isso ao sair; o `ExecStopPost` cobre `SIGKILL`/OOM; o watchdog de 30 s mata
  um daemon travado.
- **Kernel fora da série 7.0:** o DKMS não compila e o driver original
  continua funcionando.
- **Permissões:** o daemon roda como root, mas sem nenhuma *capability* e com
  sandbox do systemd. O `victusctl` roda como usuário comum: só o grupo
  `victus-thermal` e o usuário que instalou (por ACL, que vale sem precisar
  sair da sessão) podem gravar o pedido de modo. Ele é lido sem seguir links
  simbólicos e só é aceito se for uma palavra conhecida.

## Congelamentos: diagnóstico

Os avisos `WARNING: kernel/workqueue.c:2351` antes dos congelamentos indicam
que algum driver enfileirou um trabalho que já estava na fila, mas o *stack
trace* não diz qual. Para descobrir:

```bash
sudo tools/catch-workqueue-warning.sh start    # arma até o reboot
# ... use normalmente até o próximo aviso/travamento ...
sudo tools/catch-workqueue-warning.sh report   # mostra a função culpada
```

Se o culpado for o Wi-Fi (`rtw89_*`), aplique `extras/rtw89-stability.conf`.

## Desinstalar

```bash
sudo ./uninstall.sh            # remove serviço e driver, mantém a config
sudo ./uninstall.sh --purge    # remove tudo
```

## Desenvolvimento

```bash
python3 -I -m unittest discover -s tests -t .   # 50 testes, incluindo sysfs falso
make -C driver                                  # compila o módulo sem instalar
```

Enviar o patch para o kernel oficial: veja [docs/UPSTREAM.md](docs/UPSTREAM.md).

## Licença

Daemon e ferramentas: GPL-3.0-or-later. O driver (`driver/hp-wmi.c`) mantém a
licença original do kernel, GPL-2.0-or-later.
