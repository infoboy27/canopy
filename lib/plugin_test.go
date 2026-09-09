package lib

import (
	"testing"

	"github.com/stretchr/testify/require"
)

// TestPluginBeginRequestConsensusEntropyRoundTrip locks in the wire contract for
// the fair-randomness-v2 entropy fields: the FSM fills last_block_hash (and, in a
// later phase, vdf_output) from the committed predecessor header and the plugin
// must receive them byte-for-byte. A broken proto regen would drop these.
func TestPluginBeginRequestConsensusEntropyRoundTrip(t *testing.T) {
	lastHash := make([]byte, 32)
	for i := range lastHash {
		lastHash[i] = byte(i + 1)
	}
	vdf := []byte("verifiable-delay-output")

	in := &PluginBeginRequest{Height: 42, LastBlockHash: lastHash, VdfOutput: vdf}
	bz, err := Marshal(in)
	require.NoError(t, err)

	out := new(PluginBeginRequest)
	require.NoError(t, Unmarshal(bz, out))
	require.Equal(t, uint64(42), out.Height)
	require.Equal(t, lastHash, out.GetLastBlockHash())
	require.Equal(t, vdf, out.GetVdfOutput())

	// height-1 / genesis case: no predecessor, entropy fields stay empty
	genesis := &PluginBeginRequest{Height: 1}
	bz, err = Marshal(genesis)
	require.NoError(t, err)
	out = new(PluginBeginRequest)
	require.NoError(t, Unmarshal(bz, out))
	require.Nil(t, out.GetLastBlockHash())
	require.Nil(t, out.GetVdfOutput())
}
