{ system, config }:
let
  upstreamLib = import (builtins.getEnv "NIXPKGS_LIB");
  # These fixtures model a package set still supporting both Darwin targets.
  lib = upstreamLib // {
    systems = upstreamLib.systems // {
      doubles = upstreamLib.systems.doubles // {
        all = upstreamLib.systems.doubles.all ++ [ "x86_64-darwin" ];
      };
    };
  };
  package = meta: { type = "derivation"; version = "2.0"; inherit meta; };
in
assert config.allowUnsupportedSystem;
assert config.allowBroken;
{
  inherit lib;
  stdenv.hostPlatform = lib.systems.elaborate system;
  linuxOnly = package { platforms = lib.platforms.linux; };
  darwinOnly = package { platforms = [ "x86_64-darwin" "aarch64-darwin" ]; };
  armOnly = package { platforms = [ "aarch64-linux" "aarch64-darwin" ]; };
  badPlatform = package { platforms = lib.platforms.linux; badPlatforms = [ "x86_64-linux" ]; };
  broken = package { platforms = lib.platforms.all; broken = true; };
  deprecatedDarwin = assert (config.allowDeprecatedx86_64Darwin or null) == "force";
    package { platforms = [ "x86_64-darwin" "aarch64-darwin" ]; };
  evalError = throw "unexpected package failure must not become unsupported";
}
