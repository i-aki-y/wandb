// sub-package for gowandb run options
package runopts

import (
	spb "github.com/wandb/wandb/core/pkg/service_go_proto"
	"github.com/wandb/wandb/experimental/client-go/pkg/runconfig"
)

type RunParams struct {
	Config    *runconfig.Config
	Name      *string
	RunID     *string
	Project   *string
	Telemetry *spb.TelemetryRecord
}

type RunOption func(*RunParams)

func WithConfig(config runconfig.Config) RunOption {
	return func(p *RunParams) {
		p.Config = &config
	}
}

func WithName(name string) RunOption {
	return func(p *RunParams) {
		p.Name = &name
	}
}

func WithRunID(runID string) RunOption {
	return func(p *RunParams) {
		p.RunID = &runID
	}
}

func WithProject(project string) RunOption {
	return func(p *RunParams) {
		p.Project = &project
	}
}
