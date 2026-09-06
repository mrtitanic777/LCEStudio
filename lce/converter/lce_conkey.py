"""
Console signing key for STFS CON packages.

Taken from Horizon's own settings table (Resources/Horizon V2/Horizon/Server/
Config.cs, addSetting("con_*")). It is the key pair Horizon has always used to
resign console-signed saves, together with the matching 0x1A8 console
certificate that goes at offset 0x004 of the package.

con_d is shipped as 128 zero bytes there, so d is recovered from e, p and q -
which is arithmetic, and gives a value that agrees with the stored dp, dq and
inverse q.
"""

# --- as they appear in Config.cs ------------------------------------------
CON_EXPONENT = 65537

CON_MODULUS = bytes.fromhex(
    "a31d6ce5fa95fde89021fad10c64192b86589b172b1005b8d1f84cef534cd54e"
    "5cae86ef927b90d1e062fd7c54559ee0e7befa3f9e156f6c384eaf070c61ab51"
    "5e2353141888cb6fcbc5d630f406ed2423ef256d009177249be5a3c02790c297"
    "f7749d6f17837eb537de51e8d71ce156d956c8c3c3209d64c32f8c9192306fdb")

CON_P = bytes.fromhex(
    "cce75dfe72b6fde71de31a0eac337ab921e88a849bda9f1e5834687ab11d7e1c"
    "1852657b978ea76a9dee5a77523b718f33d0495ec330397236bf1dd9f224e871")

CON_Q = bytes.fromhex(
    "cbca5874d403629306501f42f6aa5936a7a1f3975c9ac86a27cf85052a66416a"
    "7f2f84c81813c61d8dc7322f72193fa4ed71e761c0cf61ae8ba068a77d83230b")

# Stored alongside p and q; used only to check the recovered d is right.
CON_DP = bytes.fromhex(
    "4cca74e67435724858621114e8a24e5eed7f49d252da8701874af4d0ee69c026"
    "655313e752b04abbe13e3fb7322146f8c5114d3def66b650c085b579458f6171")

CON_DQ = bytes.fromhex(
    "afdc46e7528a3547a11c054e392499e64354cbabe3db22761132d09cbb911084"
    "818b152fc32f5538edbf673c705eff8028f3b173b6fa7f562be1da4e274ec22f")

CON_INVERSE_Q = bytes.fromhex(
    "286abbd19395941a6eedd70ec0612bc2efe1863d3412886f94a4486ec9871e46"
    "004600528e9f47c08cabbc49ac5b13f2ec278d1b6e5106a6f1621aeb782e8848")

# The console certificate, written whole at 0x004. Carries the console id,
# part number, and the public half of the key above, signed by Microsoft.
CON_PUBLIC_KEY = bytes.fromhex(
    "01a80912ba26e3583830333339352d3030310000000000000000000230392d31"
    "382d303600010001c32f8c9192306fdbd956c8c3c3209d6437de51e8d71ce156"
    "f7749d6f17837eb59be5a3c02790c29723ef256d00917724cbc5d630f406ed24"
    "5e2353141888cb6f384eaf070c61ab51e7befa3f9e156f6ce062fd7c54559ee0"
    "5cae86ef927b90d1d1f84cef534cd54e86589b172b1005b89021fad10c64192b"
    "a31d6ce5fa95fde8f9700d2a89399ed54e658744f94f2090894137502ef83008"
    "cc6ecdd157e7c3b796b02a8059cb7e43fbdb7e0cef6c5e000b1e87e10264a708"
    "2432b9531500e9e3530c15e15d59c609abd173b5eec5e750bcc2b22598baa00a"
    "84f4f82d1ad2c97fdccf5d02219a25e069116cfc88060149f474408dd891db83"
    "c960ce0d7f97aa2a36a5f00c1063e9a9394fbb476c4422f1be3a4901ed5b4700"
    "4321bdfbb2959a5fb446f4a712244b0b7fb88ebb528322581e06b7ad7a3a167e"
    "c8d737819e8af2c4660888fea70e8f9d875f0e7b489a0662f72425cdb04f7368"
    "970ce4ade8559ab4fa65b5a358fe814054aa1f002af1dd8a1f454e9dff82465a"
    "5a9025a0580ff227")
