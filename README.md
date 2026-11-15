# RaspiWiFi para Debian com NetworkManager

Configure o Wi‑Fi de um Raspberry Pi pelo navegador. Quando o aparelho fica sem conexão Wi‑Fi com IPv4, o RaspiWiFi pode abrir um hotspot local para você escolher a rede à qual o Pi deve se conectar.

Esta adaptação usa NetworkManager no Debian 12 e no Raspberry Pi OS Bookworm. O fluxo descrito aqui foi testado em um Raspberry Pi 3 Model B com Debian 12. A validação em uma imagem limpa e com dois celulares simultâneos no hotspot segue pendente.

## Instalação

O aparelho precisa de uma interface Wi‑Fi com suporte a ponto de acesso, NetworkManager ativo, `systemd` e acesso aos repositórios APT caso alguma dependência esteja ausente. No diretório do projeto, execute no Raspberry:

```bash
sudo /usr/bin/python3 -I -S initial_setup.py
```

O instalador mantém a conexão Wi‑Fi atual. O SSID inicial é `RaspWifi`. Pressione Enter no campo de senha para gerar uma senha WPA2 exclusiva por instalação ou digite uma senha própria. A ativação automática vem habilitada com espera de **300 segundos sem Wi‑Fi com IPv4**. O instalador também oferece acesso opcional ao PiKaraoke pela porta `5555`.

Para atualizar uma instalação deste projeto que já usa NetworkManager, execute o comando abaixo enquanto o Pi estiver na rede normal. A migração da instalação anterior ao portal protegido gera uma nova senha para o hotspot.

```bash
sudo /usr/bin/python3 -I -S initial_setup.py --upgrade
```

Se o aparelho usa a versão legada com `hostapd` e `dhcpcd`, remova-a com o desinstalador da versão original antes de instalar esta adaptação.

## Abrir o portal

1. Pelo console ou pelo SSH administrativo, consulte a senha WPA2 e o código administrativo. Se escolheu outro SSID, troque `RaspWifi` no nome do perfil.

   ```bash
   sudo nmcli --show-secrets -g 802-11-wireless-security.psk \
     connection show id 'RaspiWiFi hotspot: RaspWifi'
   sudo cat /etc/raspiwifi/admin-code
   ```

2. Obtenha o certificado público da CA em `/etc/raspiwifi/portal-ca.pem` pelo console ou por uma sessão SSH verificada. Configure o dispositivo administrador para confiar nesse certificado antes de abrir o portal. A chave privada fica no Raspberry. O certificado do portal vale por um ano e identifica `acesso.local` e `10.0.0.1`.

3. Aguarde o hotspot automático ou solicite a ativação pelo console local ou por SSH via Ethernet. Trocar a interface Wi‑Fi para o modo hotspot derruba sessões SSH que dependem dela.

   ```bash
   sudo /usr/bin/python3 /usr/lib/raspiwifi/reset_device/manual_reset.py
   ```

4. Conecte o dispositivo à rede **RaspWifi**. Abra [https://acesso.local](https://acesso.local). Se o dispositivo não resolver mDNS, use [https://10.0.0.1](https://10.0.0.1). Os endereços HTTP exibem apenas a página de entrada com o link para HTTPS. Confirme o endereço e a confiança no certificado antes de digitar o código administrativo.

5. Após entrar, escolha uma rede ou informe o SSID manualmente. O portal aceita redes abertas e WPA Personal, incluindo SSID oculto por entrada manual. O hotspot cai enquanto o Pi testa a nova conexão. O perfil só é salvo depois de obter IPv4. Se o hotspot voltar, reconecte-se a ele e tente novamente. A troca não exige reiniciar o Raspberry.

## Segurança e administração

- O portal recebe o código administrativo e as credenciais Wi‑Fi somente por HTTPS. O servidor web roda sem `root`, e o serviço de rede executa as alterações no NetworkManager.
- O hotspot bloqueia SSH e encaminhamento para outras redes, e habilita isolamento entre clientes. O mDNS anuncia `acesso.local` enquanto o hotspot estiver ativo.
- Para habilitar o PiKaraoke após a instalação, defina `"karaoke": true` em `/etc/raspiwifi/networkmanager.json` e reinicie `raspiwifi.service` pela rede normal. Com o PiKaraoke em execução, use `http://10.0.0.1:5555` no hotspot.
- Na rede normal, configure o SSH administrativo por chave. Teste uma segunda sessão por chave antes de desativar a autenticação por senha. A configuração do SSH da rede normal é responsabilidade do administrador do Raspberry.

Para consultar os logs ou remover o RaspiWiFi:

```bash
sudo journalctl -u raspiwifi.service -u raspiwifi-landing.service -u raspiwifi-web@https.service
sudo /usr/bin/python3 -I -S /usr/lib/raspiwifi/uninstall.py
```

A remoção exclui as configurações e os perfis de rede criados pelo RaspiWiFi. Use o console ou Ethernet se estiver acessando o aparelho pela rede que será removida.

## Projeto original e licença

Adaptação do [RaspiWiFi de Jason Burgett](https://github.com/jasbur/RaspiWiFi), sob GPL-3.0 ou posterior. Consulte [COPYING](COPYING) e [gpl.txt](gpl.txt). Para informações sobre a imagem Raspbian Stretch, o reset por GPIO e a versão legada, consulte o repositório original.
