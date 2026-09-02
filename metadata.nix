{ nixpkgs, attribute }:
let
  systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  inspect = system:
    let
      pkgs = import (builtins.toPath nixpkgs) {
        inherit system;
        config = {
          allowUnsupportedSystem = true;
          # Inspect metadata on Nixpkgs 26.11 despite the import-level removal;
          # lib.meta.availableOn still controls eligibility. Never build here.
          allowDeprecatedx86_64Darwin = "force";
          allowBroken = true;
          allowUnfree = true;
          allowAliases = true;
          allowInsecurePredicate = _: true;
        };
      };
      path = pkgs.lib.splitString "." attribute;
      package = pkgs.lib.attrByPath path (throw "Missing attribute: ${attribute}") pkgs;
      available = pkgs.lib.meta.availableOn pkgs.stdenv.hostPlatform package;
      broken = package.meta.broken or false;
      # A lingering explicit meta.platforms entry cannot resurrect a target
      # removed from Nixpkgs' global system declaration. The runner does not
      # receive the metadata-only deprecated-platform override above.
      supported = builtins.elem system pkgs.lib.systems.doubles.all;
    in
    assert pkgs.lib.assertMsg (pkgs.lib.isDerivation package) "Attribute is not a derivation";
    {
      version = package.version or (throw "Package has no version");
      eligible = supported && available && !broken;
      reason = if !supported then "Nixpkgs no longer declares support for this system" else if broken then "meta.broken" else if !available then "meta.platforms or meta.badPlatforms excludes system" else "available";
    };
  inspected = builtins.listToAttrs (map (system: { name = system; value = inspect system; }) systems);
  versions = builtins.attrValues (builtins.mapAttrs (_: row: row.version) inspected);
  version = builtins.head versions;
in
assert builtins.all (value: value == version) versions;
{
  inherit version;
  systems = builtins.mapAttrs (_: row: { inherit (row) eligible reason; }) inspected;
}
