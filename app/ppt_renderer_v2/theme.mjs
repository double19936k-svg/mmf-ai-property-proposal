export const W = 1280;
export const H = 720;

export const THEME = Object.freeze({
  id: "Theme D V1.1",
  status: "frozen",
  source: "Stage1 approved/frozen Theme-D_视觉参数_V1.1.json",
  colors: {
    deep: "#0F3943",
    primary: "#174B58",
    medium: "#3F6670",
    secondary: "#56747B",
    light: "#DCE7E7",
    soft: "#EAF0F0",
    pale: "#F2F6F5",
    background: "#F7F9F8",
    panel: "#FFFFFF",
    ink: "#1D3035",
    muted: "#61747A",
    line: "#CBD7D8",
    grid: "#E4EAEB",
    white: "#FFFFFF",
    gold: "#B28A55"
  },
  fonts: {
    title: "Microsoft YaHei",
    subtitle: "DengXian",
    body: "Microsoft YaHei",
    numeric: "Microsoft YaHei"
  },
  typography: {
    deckTitle: 52,
    slideTitle: 35,
    lead: 16,
    cardTitle: 18,
    body: 16,
    caption: 13
  }
});

export function configureTheme(presentation) {
  const c = THEME.colors;
  presentation.theme.colorScheme = {
    name: THEME.id,
    themeColors: {
      accent1: c.primary,
      accent2: c.secondary,
      accent3: c.medium,
      accent4: c.deep,
      accent5: c.light,
      accent6: c.soft,
      bg1: c.background,
      bg2: c.panel,
      tx1: c.ink,
      tx2: c.muted,
      dk1: "#000000",
      dk2: c.deep,
      lt1: c.white,
      lt2: c.pale,
      hlink: c.primary,
      folHlink: c.secondary
    }
  };
}
