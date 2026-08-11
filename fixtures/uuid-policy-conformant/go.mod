// Fixture only; see the violating fixture's go.mod. This tree must produce ZERO
// findings. Every file in it is a place a non-v7 UUID is legitimately fine, so a
// finding here is a false positive and the self-test fails on it.
module example.com/uuid-policy-conformant

go 1.22
