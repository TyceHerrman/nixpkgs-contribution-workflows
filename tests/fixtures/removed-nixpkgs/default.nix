args:
let
  packages = import ../nixpkgs args;
  upstreamLib = import (builtins.getEnv "NIXPKGS_LIB");
in
packages // {
  lib = packages.lib // {
    systems = packages.lib.systems // {
      doubles = packages.lib.systems.doubles // {
        all = builtins.filter (system: system != "x86_64-darwin") upstreamLib.systems.doubles.all;
      };
    };
  };
}
