declare module "qrcode-terminal" {
  export type ErrorCorrectionLevel = "L" | "M" | "Q" | "H";

  export interface GenerateOptions {
    small?: boolean;
  }

  export function setErrorLevel(level: ErrorCorrectionLevel): void;
  export function generate(
    text: string,
    options?: GenerateOptions,
    cb?: (qrcode: string) => void
  ): void;

  const qrcode: {
    setErrorLevel: typeof setErrorLevel;
    generate: typeof generate;
  };

  export default qrcode;
}
