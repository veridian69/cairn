package chat

import (
	"sort"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

const (
	colorBg      = lipgloss.Color("#262624")
	colorSurface = lipgloss.Color("#30302E")
	colorLine    = lipgloss.Color("#3E3D3A")
	colorText    = lipgloss.Color("#F0EEE6")
	colorMuted   = lipgloss.Color("#A6A39A")
	colorCoral   = lipgloss.Color("#D97757")
	colorError   = lipgloss.Color("#C25E4C")
)

var agentPalette = []lipgloss.Color{
	lipgloss.Color("#6A9BCC"),
	lipgloss.Color("#7D9B76"),
	lipgloss.Color("#C2A878"),
	lipgloss.Color("#B58DAE"),
	lipgloss.Color("#6FADA0"),
}

var (
	styleText      = lipgloss.NewStyle().Foreground(colorText)
	styleMuted     = lipgloss.NewStyle().Foreground(colorMuted)
	styleError     = lipgloss.NewStyle().Foreground(colorError)
	styleRedacted  = lipgloss.NewStyle().Foreground(colorMuted).Italic(true)
	styleChip      = lipgloss.NewStyle().Background(colorCoral).Foreground(colorBg).Padding(0, 1)
	styleIdentity  = lipgloss.NewStyle().Background(colorSurface).Foreground(colorText).Padding(0, 1)
	styleStatusBar = lipgloss.NewStyle().Background(colorSurface)

	styleComposerFocused = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(colorCoral).
				Background(colorBg)
	styleComposerBlurred = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(colorLine).
				Background(colorBg)
)

type colourMap struct {
	human    string
	assigned map[string]lipgloss.Color
	next     int
}

func newColourMap(human string, agents []string) *colourMap {
	colours := &colourMap{
		human:    human,
		assigned: make(map[string]lipgloss.Color),
	}
	sorted := append([]string(nil), agents...)
	sort.Strings(sorted)
	for _, name := range sorted {
		name = strings.TrimSpace(name)
		if name == "" || name == human {
			continue
		}
		if _, exists := colours.assigned[name]; exists {
			continue
		}
		colours.assign(name)
	}
	return colours
}

func (c *colourMap) assign(name string) lipgloss.Color {
	colour := agentPalette[c.next%len(agentPalette)]
	c.assigned[name] = colour
	c.next++
	return colour
}

func (c *colourMap) colour(name string) lipgloss.Color {
	if name == c.human {
		return colorCoral
	}
	if colour, ok := c.assigned[name]; ok {
		return colour
	}
	return c.assign(name)
}
