# Como funciona

Este documento explica as decisões técnicas do projeto. Para usar, basta o
[README](../README.md).

## 1. O driver

O `hp-wmi` do kernel escolhe o protocolo térmico pela placa (`DMI_BOARD_NAME`),
numa tabela. A 8C2F usa o protocolo **Victus S**, o mesmo das placas 8BBE,
8BD4, 8BD5, 8C99, 8C9C e 8D41. Isso foi confirmado de forma independente pelo
omen-fan-control v2.1.0, testado no 15-fb2xxx.

`driver/hp-wmi.c` é **cópia exata** do arquivo do pacote `linux-source-7.0.0`
(Ubuntu 7.0.0-38.38), mais uma entrada na tabela:

```c
{
	.matches = { DMI_MATCH(DMI_BOARD_NAME, "8C2F") },
	.driver_data = (void *)&victus_s_thermal_params,
},
```

`victus_s_thermal_params` usa `HP_EC_OFFSET_UNKNOWN`: o driver **nunca lê nem
escreve registradores do controlador embarcado (EC)**. Tudo passa por métodos
WMI que a BIOS implementa e valida. Com isso o driver ganha:

| Recurso | Como aparece |
|---|---|
| Modos térmicos HP | `platform_profile`: `low-power`, `balanced`, `performance` |
| Boost da GPU (cTGP / PPAB) | junto com o perfil |
| Limites de potência da CPU | voltam ao padrão quando muda a fonte de energia |
| RPM das ventoinhas | `fan1_input`, `fan2_input` |
| Controle manual | `pwm1_enable` (0 = máximo, 1 = manual, 2 = firmware) e `pwm1` |
| Luz do teclado¹ | LED `hp::kbd_backlight` (0/1), via UPower para o GNOME |

¹ Acréscimo local, ainda não enviado ao kernel oficial. Usa o comando WMI
`0x20009` do OMEN: subcomando 4 lê o registrador de brilho do teclado no EC,
5 grava e manda o EC aplicar (bit 7 = aceso; `0x64` apaga, `0xE4` acende),
conforme os métodos `LM04`/`LM05` da DSDT da 8C2F. O evento `0x0D` (atalho Fn)
vira `brightness_hw_changed` para o UPower. Só é registrado em placas
`victus_s` cuja BIOS liga o bit 0 da consulta de capacidades (subcomando 1,
método `LM01`); a resposta vai para o log (`keyboard backlight: caps … state
…`). **Na 8C2F esse bit é 0** (`caps [06 3c]`): os comandos são aceitos e o
valor é lido de volta, mas o controlador não acende nem apaga a luz branca,
que só responde ao atalho Fn. Por isso o LED não é registrado nela.

O firmware reverte o modo manual depois de 120 s; o próprio driver reenvia o
comando a cada 90 s (*keep-alive*).

**DKMS:** o `dkms.conf` limita a compilação ao kernel `7.0.x`, porque as APIs
internas mudam entre séries. Em outro kernel nada é compilado, e o driver
original do Ubuntu continua funcionando. O DKMS do Ubuntu assina o módulo com
a chave MOK em `/var/lib/shim-signed/mok/`.

**Teste antes de instalar:** o `driver-test` compila e assina o módulo,
descarrega o original, carrega as dependências declaradas (`sparse-keymap`,
`wmi`, `platform_profile`), insere o novo e confere a interface de ventoinhas,
o `pwm1`, o registro do perfil e os erros do kernel. Qualquer falha devolve o
driver original.

## 2. O serviço (`victus-thermald`)

Um loop a cada 2 s: lê sensores, decide e aplica. Entre um passo e outro ele
verifica, a cada 0,2 s, se o modo pedido, a preferência da tomada ou o perfil
mudaram. Se mudaram, decide na hora.

### Ordem das decisões (`policy.py`)

1. **Emergência:** algum sensor ≥ `emergency_temp` (leitura bruta, sem
   suavização) → **máximo**, até todos ≤ `emergency_release`.
2. **Modo forçado pelo usuário:** `auto` → firmware; `max` → máximo, se o
   perfil estiver em `max_profiles`. Fora deles, o pedido é apagado e o modo
   volta a `curve`.
3. **Sem sensor de CPU** → firmware.
4. **Perfil configurado como `"firmware"`** → firmware.
5. **Curva:** entrada = sensor mais quente (CPU, iGPU, NVIDIA), suavizado
   (média exponencial) e com histerese na descida.
   - **Na tomada com `ac_quiet`:** nunca abaixo de `ac_quiet_duty`.
   - **Curva em 0 %:** firmware, ou velocidade baixa enquanto durar o
     `hold_time` depois de a curva ter ligado as ventoinhas.
   - **Assumir do firmware:** só depois de `engage_delay` s de demanda
     contínua, medida na temperatura suavizada **sem** histerese (com
     histerese, cada pico pareceria sustentado). Curva a 100 % assume na hora.
   - **Curva em 100 %** → máximo; senão, manual com o `pwm` da curva.

### Aplicação (`daemon.py`)

- Ao passar de **firmware para manual**, lê a velocidade atual do firmware
  (`pwm1`) e usa como **piso**: a curva nunca assume mais devagar do que o
  firmware estava girando.
- Mudanças de `pwm` menores que 3/255 não são gravadas (cada gravação é uma
  chamada WMI).
- Ao sair, por qualquer motivo, grava `pwm1_enable = 2` (firmware). O
  `ExecStopPost` repete isso para os casos em que o Python não consegue.

### Custos medidos no 8C2F

| Leitura | Custo | Como é tratada |
|---|---|---|
| `fanN_input` (RPM) | **~310 ms** de BIOS cada, segurando o interpretador ACPI | cache de 10 s, mais uma leitura 3 s depois de cada mudança |
| `pwm1_enable` | ~0,1 ms | toda decisão |
| temperaturas (`k10temp`, `amdgpu`) | < 2 ms | toda decisão |
| `nvidia-smi` | ~22 ms de CPU, e conta como atividade da GPU | veja abaixo |

Resultado: ~1% de CPU e ~13% de tempo de BIOS (antes da otimização: ~6% e
~50%).

### NVIDIA sem acordar

Cada consulta ao `nvidia-smi` reinicia o tempo até a GPU dormir. Por isso
(`DgpuSampler` em `hw.py`):

- GPU suspensa → nunca consulta;
- acordou há menos de 10 s → não consulta (provavelmente vai dormir de novo);
- acordada e ociosa → no máximo a cada 30 s;
- em uso → a cada 5 s.

## 3. Permissões

| Caminho | Dono | Conteúdo |
|---|---|---|
| `/run/victus-thermal/status.json` | root, 0644 | estado atual (leitura para todos) |
| `/run/victus-thermal/control/mode` | grupo `victus-thermal` + ACL do usuário | pedido de modo; apagado a cada reinício |
| `/var/lib/victus-thermal/ac-quiet` | grupo `victus-thermal` + ACL do usuário | preferência lembrada (`on`/`off`) |
| `/etc/victus-thermal/config.toml` | root | configuração |

Os arquivos de pedido são **dados não confiáveis**: o serviço os abre com
`O_NOFOLLOW`, lê no máximo 16 bytes e só aceita palavras conhecidas. A ACL do
usuário que instalou vale na hora; o grupo só vale a partir do próximo login.

A unidade systemd roda com `CapabilityBoundingSet=` vazio, `ProtectSystem=strict`,
`PrivateNetwork`, `NoNewPrivileges`, `MemoryDenyWriteExecute` e filtro de
syscalls, e com `WatchdogSec=30`.

## 4. Extensão do GNOME

Lê `status.json` com um monitor de arquivo (o serviço substitui o arquivo por
rename atômico), então atualiza ~1–3 ms depois de cada decisão. Um timer de
5 s só serve para detectar um serviço parado. As ações chamam `victusctl`; a
extensão não tem nenhum privilégio.

## 5. Luz do teclado na extensão

`keyboardLight.js` fala com o UPower (`org.freedesktop.UPower.KbdBacklight`)
e usa o monitor de inatividade do Mutter: depois de `kbd-auto-off-seconds`
sem entrada, apaga a luz se ela estiver acesa e guarda o nível; na primeira
entrada, acende de novo 0,4 s depois. O atraso garante que essa ação venha
depois da restauração que o próprio GNOME faz ao acordar a tela. Uma luz
apagada pelo usuário nunca é acesa. As preferências ficam em GSettings
(`org.gnome.shell.extensions.victus-thermal`).

## 6. NVIDIA sob demanda

O loader Vulkan carrega **todos** os drivers instalados quando qualquer app
Vulkan abre, e o driver NVIDIA abre a GPU ao carregar (medido: 15 aberturas de
`/dev/nvidia*` por processo). `extras/gpu-on-demand` define
`VK_LOADER_DRIVERS_SELECT=*radeon*` na sessão. Os lançadores de jogos ganham
`PrefersNonDefaultGPU=true`: o GNOME os inicia com o ambiente do
switcheroo-control para a NVIDIA, que sobrescreve a mesma variável com
`*nvidia*` e liga o PRIME offload do OpenGL.

A porta HDMI do notebook está ligada à NVIDIA, por isso o GNOME mantém o
dispositivo aberto; isso não impede a GPU de dormir.
