{
  description = "Catalogue importer with Camoufox runtime libraries";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      browserLibraries = with pkgs; [
        alsa-lib cairo dbus-glib fontconfig freetype glib gtk3 libX11 libxcb
        libXcomposite libXdamage libXext libXfixes libXrandr libXt nspr nss pango
      ];
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ pkgs.uv ];
        LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath browserLibraries;
      };
    };
}
