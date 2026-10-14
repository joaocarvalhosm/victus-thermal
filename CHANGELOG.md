# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

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
