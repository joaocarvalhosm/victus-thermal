# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [1.9.2] - 2026-10-24
### Corrigido
- **Extensão:** ao ser desligada (por exemplo, ao bloquear a tela), ela às
  vezes ainda atualizava widgets já destruídos, deixando erros "has been
  already disposed" no log. Uma leitura do status ou um `victusctl` que
  terminava logo antes do cancelamento seguia em frente. Agora o indicador e
  o botão turbo conferem se foram cancelados depois de cada espera.

## [1.9.1] - 2026-10-24
### Corrigido
- **Kernel com failover:** o initrd do kernel genérico parava de ser
  atualizado. O `update-initramfs` só refaz o kernel "mais novo" pela
  numeração, e o OEM (`7.0.0-1015`) fica acima do genérico (`7.0.0-38`).
  O instalador agora grava `update_initramfs=all` e refaz todos os initrd.

### Adicionado
- `extras/kernel-failover/install.sh --status`: o GRUB anota a cada boot o
  kernel que planejou subir (`victus_planned` no `grubenv`), e o `--status`
  compara com o kernel em uso. Assim dá para distinguir um failover de uma
  escolha no menu ou de um kernel que o GRUB não conseguiu carregar.

## [1.9.0] - 2026-10-16
### Adicionado
- **Botão OMEN = turbo:** o botão OMEN (`KEY_PROG2`, "Launch2" no GNOME)
  alterna entre o perfil Desempenho com as ventoinhas no Máximo e o perfil
  Equilibrado com as ventoinhas no Automático. A tecla pode ser mudada com a
  chave `turbo-shortcut`.
- Avisos do turbo: uma mensagem no alto e no centro da tela. Ao ligar, a
  ventoinha da barra gira por 8 s, acelerando aos poucos, e para alinhada.
  Com o turbo ligado, ela fica na cor de destaque. Os avisos esperam o GNOME
  colocar ou tirar o ícone de Desempenho dele, para a barra não pular no
  meio da animação.
- O ícone da barra troca de cor (turbo, quente, muito quente) com
  esmaecimento suave em vez de mudar de uma vez.
- `victusctl turbo [on|off|toggle]`: o mesmo pelo terminal. Ele espera o
  perfil Desempenho valer antes de pedir o Máximo, que o serviço recusa em
  outros perfis.

## [1.8.1] - 2026-10-16
### Adicionado
- `extras/kernel-failover`: o kernel genérico mais novo é o padrão, e o
  kernel OEM assume sozinho no boot seguinte a um boot que falhou (usa o
  `recordfail` do GRUB do Ubuntu, que é apagado em todo boot completo).
- `tools/workqueue-watch.sh`: serviço que liga o rastreamento do
  `workqueue.c:2351` em todo boot e grava a função culpada em
  `/var/log/victus-workqueue/` no instante do aviso. A ferramenta manual
  perdia o resultado no reinício forçado que sempre segue esse aviso.
### Documentado
- **Causa dos travamentos totais:** o pedido de permissão do AppArmor dos
  snaps (bug do kernel 6.17+/7.0, LP #2143937), disparado ao baixar ou
  salvar arquivos no Brave e no Firefox. Desligá-lo resolveu. O README
  explica o sintoma e a solução. O hardware, o driver NVIDIA, o Wi-Fi, o SSD
  e este projeto foram descartados.

## [1.8.0] - 2026-10-14
### Adicionado
- **Período de inicialização** (`startup_grace`, 90 s): logo depois que o
  serviço sobe (no boot), as ventoinhas ficam com o firmware da HP. A carga
  do boot aquece a CPU por alguns instantes, e a curva acelerava as
  ventoinhas enquanto o desktop carregava. A emergência e um modo escolhido
  manualmente continuam valendo. A extensão mostra "Iniciando: firmware HP no
  controle".
### Corrigido
- O `config.toml` padrão da 1.6.0–1.7.2 não estava na lista de arquivos não
  editados, então o instalador não o atualizaria; agora está.

## [1.7.2] - 2026-10-14
### Corrigido
- **O serviço não iniciava no boot:** a unidade pedia para iniciar depois do
  `power-profiles-daemon`, que no Ubuntu 26.04 inicia depois do
  `multi-user.target`; o systemd quebrava o ciclo cancelando o
  `victus-thermal`. A dependência (desnecessária) foi removida.
### Alterado
- Novo menu da extensão no padrão do GNOME: cabeçalho com a temperatura e o
  resumo, sensores em linhas, **Modo de energia** e **Ventoinhas** como
  submenus com o valor atual e os ícones de energia do GNOME, botão
  **Iniciar serviço** quando ele está parado (pede a senha na janela do
  GNOME). Textos secundários respeitam os temas claro e escuro.
- `install.sh` e `uninstall.sh` aceitam `pkexec` além de `sudo`.
- `tools/catch-workqueue-warning.sh report` usa o primeiro aviso depois de
  armado, não o primeiro do boot.

## [1.7.1] - 2026-10-14
### Corrigido
- O driver (DKMS 1.2.0) só registra a luz do teclado quando a BIOS informa
  que ela é controlável pelo comando WMI `0x20009` (bit 0 da consulta de
  capacidades). Na 8C2F, com teclado branco, esse bit é 0: a BIOS aceita os
  comandos, mas o controlador os ignora, e a luz só responde ao atalho Fn.
  Por isso a luz não apagava, e o GNOME ficava gravando um valor sem efeito
  no controlador. Agora nada é registrado nem gravado nesse modelo, e o botão
  "Luz do teclado" não aparece.

## [1.7.0] - 2026-10-14
### Adicionado
- **Luz do teclado:** o driver (DKMS 1.1.0) expõe `hp::kbd_backlight`
  (liga/desliga) pela interface de iluminação do OMEN (WMI `0x20009`),
  confirmada nas tabelas ACPI da 8C2F. O GNOME passa a apagar a luz quando a
  tela apaga, e o atalho Fn é repassado a ele.
- Botão **Luz do teclado** nas configurações rápidas do GNOME: apaga a luz
  depois de 30 s / 1 min / 2 min / 5 min sem uso e acende ao voltar. Nunca
  acende uma luz que você apagou manualmente.
### Alterado
- `install.sh all` atualiza o driver quando a versão do DKMS muda (antes
  pulava qualquer driver já instalado) e reinicia o UPower.
- Extensão dividida em módulos (`thermalIndicator.js`, `keyboardLight.js`).

## [1.6.0] - 2026-10-13
### Alterado
- Modo **Máximo** só é permitido no perfil Desempenho (configurável com
  `max_profiles`). Trocar para Economia ou Equilibrado volta para Automático.
  A emergência continua sem restrição.
- Extensão mostra "Máximo (só no Desempenho)" desabilitado nos outros perfis.

## [1.5.0] - 2026-10-13
### Adicionado
- **Silencioso contínuo na tomada** (`ac_quiet`, ligado por padrão): com o
  carregador ligado, as ventoinhas nunca param. Chave na extensão e
  `victusctl ac-quiet on|off`, lembrados entre reinícios.

## [1.4.0] - 2026-10-13
### Adicionado
- Filtro de picos (`smoothing` 0,25), atraso para assumir (`engage_delay`
  6 s) e tempo mínimo ligado (`hold_time` 90 s), contra o liga-desliga do
  firmware.
- Curvas entram um pouco antes do firmware, em velocidade baixa.

## [1.3.0] - 2026-10-13
### Corrigido
- O serviço lia o RPM (~310 ms de BIOS cada) a cada 2 s: a BIOS ficava
  ocupada ~50% do tempo. Agora o RPM fica em cache por 10 s: ~1% de CPU.
- O `nvidia-smi` a cada 2 s impedia a NVIDIA de dormir.
### Adicionado
- `extras/gpu-on-demand`: NVIDIA só sob demanda, com jogos abrindo na RTX.

## [1.2.0] - 2026-10-13
### Alterado
- Cliques e trocas de perfil aplicados em menos de 0,5 s; extensão atualiza
  em ~1–3 ms (monitor de arquivo).
- Modos renomeados: Automático (recomendado), Somente firmware HP, Máximo.

## [1.1.0] - 2026-10-13
### Corrigido
- A curva puxava as ventoinhas abaixo do firmware ao entrar em Desempenho.
  Agora o firmware manda em carga leve, e a curva nunca assume mais devagar
  do que ele estava girando.
- "Permissão negada" até o próximo login: ACL para o usuário que instalou.
### Adicionado
- `install.sh all` também instala a extensão e atualiza configs não editadas.

## [1.0.0] - 2026-10-13
### Adicionado
- Driver `hp-wmi` com a placa 8C2F (DKMS, assinado com MOK) e patch para o
  kernel oficial.
- Serviço com curvas por perfil, emergência, watchdog e retorno ao firmware.
- `victusctl`, extensão do GNOME, instalador em etapas e desinstalador.
- Ferramenta de diagnóstico dos avisos `workqueue.c:2351`.
### Corrigido (instalador)
- Chave MOK cadastrada era tratada como ausente (`mokutil` sai com erro 1).
- `driver-test` falhava com `Unknown symbol` (dependências do módulo).
