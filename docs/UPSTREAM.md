# Sending the 8C2F patch upstream

`driver/patches/0001-platform-x86-hp-wmi-Add-Victus-15-fb2xxx-8C2F-suppor.patch`
is formatted against mainline `master` (which uses the newer
`hp_wmi_feature_boards[]` table with `victus_s_board_params`). Once it is
merged, every distro kernel picks it up and this DKMS module can be retired.

## Before sending

1. **Test on the hardware first** (`sudo ./install.sh driver-test`, then a
   gaming session in each profile). Do not send an untested patch.
2. Add your tags to the commit message:
   - `Tested-by: Your Name <email>`
   - `Signed-off-by: Your Name <email>` — the Developer Certificate of
     Origin, a legal statement; it must be your real name and only you can
     add it. (`git commit -s` / `git format-patch -s`.)
   - Kernel policy for AI-assisted patches
     (`Documentation/process/coding-assistants.rst`) asks for an
     `Assisted-by:` tag instead of `Co-Authored-By:`; adjust the trailer
     accordingly.
3. Rebase onto the current `platform-drivers-x86` tree and make sure it still
   applies and builds:
   ```bash
   git clone https://git.kernel.org/pub/scm/linux/kernel/git/pdx86/platform-drivers-x86.git
   cd platform-drivers-x86 && git checkout -b victus-8c2f origin/for-next
   git am /path/to/0001-*.patch
   ./scripts/checkpatch.pl --strict -g HEAD
   make M=drivers/platform/x86/hp
   ```

## Sending

```bash
./scripts/get_maintainer.pl -f drivers/platform/x86/hp/hp-wmi.c
git send-email --to=platform-driver-x86@vger.kernel.org \
    --cc=<maintainers from get_maintainer.pl> HEAD^..HEAD
```

Expect review questions about whether 8C2F matches `victus_s` behaviour; the
`driver-test` results (profiles, RPM readout, manual pwm working) answer them.
Mention the independent confirmation from omen-fan-control v2.1.0.
