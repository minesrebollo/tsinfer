

import subprocess
import os
import numpy as np
import itertools
import multiprocessing
import pandas as pd
import random
import statistics
import sys
import attr
import collections
import time
# import profilehooks

import humanize

import tsinfer
import _tsinfer
import msprime



def make_errors(v, p):
    """
    For each sample an error occurs with probability p. Errors are generated by
    sampling values from the stationary distribution, that is, if we have an
    allele frequency of f, a 1 is emitted with probability f and a
    0 with probability 1 - f. Thus, there is a possibility that an 'error'
    will in fact result in the same value.
    """
    w = np.copy(v)
    if p > 0:
        m = v.shape[0]
        frequency = np.sum(v) / m
        # Randomly choose samples with probability p
        samples = np.where(np.random.random(m) < p)[0]
        # Generate observations from the stationary distribution.
        errors = (np.random.random(samples.shape[0]) < frequency).astype(int)
        w[samples] = errors
    return w

def generate_samples(ts, error_p):
    """
    Returns samples with a bits flipped with a specified probability.

    Rejects any variants that result in a fixed column.
    """
    S = np.zeros((ts.sample_size, ts.num_mutations), dtype=np.int8)
    for variant in ts.variants():
        done = False
        # Reject any columns that have no 1s or no zeros
        while not done:
            S[:,variant.index] = make_errors(variant.genotypes, error_p)
            s = np.sum(S[:, variant.index])
            done = 0 < s < ts.sample_size
    return S


def sort_ancestor_slice(A, p, start, end, sort_order, depth=0):
    if end - start > 1:
        print("  " * depth, "Sort Ancestor slice:", start, ":", end, sep="")
        m = A.shape[1]
        for l in range(m):
            col = A[p,l]
            # TODO finish
            # if col[
            print(A[:,l])
            print(A[p,l])
            print()


def sort_ancestors(A, p):
    """
    Sorts the specified array of ancestors to maximise the effectiveness
    of the run length encoding.
    """
    n, m = A.shape
    p[:] = np.arange(n)
    for j in range(n):
        a = "".join(str(x) if x != -1 else '*' for x in A[j])
        print(j, "\t", a)
    sort_ancestor_slice(A, p, 0, n, 0, 0)

def build_ancestors(n, L, seed):

    ts = msprime.simulate(
        n, length=L, recombination_rate=1e-8, mutation_rate=1e-8,
        Ne=10**4, random_seed=seed)
    # print("num_sites = ", ts.num_sites)
    # print("simulation done, num_sites = ", ts.num_sites)

    position = [site.position for site in ts.sites()]

    S = np.zeros((ts.sample_size, ts.num_sites), dtype="i1")
    for variant in ts.variants():
        S[:, variant.index] = variant.genotypes

    builder = _tsinfer.AncestorBuilder(S, position)
    store = _tsinfer.AncestorStore(builder.num_sites)
    store.init_build(1024)

    for frequency, focal_sites in builder.get_frequency_classes():
        num_ancestors = len(focal_sites)
        A = np.zeros((num_ancestors, builder.num_sites), dtype=np.int8)
        p = np.zeros(num_ancestors, dtype=np.int32)
        # print("frequency:", frequency, "sites = ", focal_sites)
        for j, focal_site in enumerate(focal_sites):
            builder.make_ancestor(focal_site, A[j, :])
            # print(focal_site, ":", A[j])
        # sort_ancestors(A, p)
        # for j in range(num_ancestors):
        #     store.add(A[p[j], :])
        for j in range(num_ancestors):
            store.add(A[j,:])

    print("num sites        :", store.num_sites)
    print("num ancestors    :", store.num_ancestors)
    print("max_segments     :", store.max_num_site_segments)
    print("mean_segments    :", store.total_segments / store.num_sites)
    print("expands          :", store.num_site_segment_expands)
    print("Memory           :", humanize.naturalsize(store.total_memory))
    print("Uncompressed     :", humanize.naturalsize(num_ancestors * store.num_sites))
    print("Sample memory    :", humanize.naturalsize(S.nbytes))

    # matcher = _tsinfer.AncestorMatcher(store, 0.01, 1e-200)
    # # print(store.num_sites, store.num_ancestors)
    # h = np.zeros(store.num_sites, dtype=np.int8)
    # P = np.zeros(store.num_sites, dtype=np.int32)
    # M = np.zeros(store.num_sites, dtype=np.uint32)
    # for j in range(store.num_ancestors):
    #     store.get_ancestor(j, h)
    #     # a = "".join(str(x) if x != -1 else '*' for x in h)
    #     # print(j, "\t", a)
    #     num_mutations = matcher.best_path(store.num_ancestors, h, P, M)
    #     assert num_mutations == 0
    #     # print(P)
    # print("Matched ancestors")
    # for h in S:
    #     # print(h)
    #     num_mutations = matcher.best_path(store.num_ancestors, h, P, M)
    #     # print("num_mutation = ", num_mutations)
    #     # print(P)

        # a = "".join(str(x) if x != -1 else '*' for x in A)
        # print(a)

    # print(H2)
    # print(np.all(H1 == H2))
    # print(np.where(H1 != H2))
    # print(H1 == H2)


def new_segments(n, L, seed):

    np.set_printoptions(linewidth=2000)
    np.set_printoptions(threshold=20000)
    np.random.seed(seed)

    ts = msprime.simulate(
        n, length=L, recombination_rate=0.5, mutation_rate=1, random_seed=seed)
    if ts.num_sites == 0:
        print("zero sites; skipping")
        return
    positions = [site.position for site in ts.sites()]
    S = generate_samples(ts, 0.01)
    S2 = np.zeros((ts.sample_size, ts.num_mutations), dtype=np.int8)
    for variant in ts.variants():
        S2[:,variant.index] = variant.genotypes

    # tsp = tsinfer.infer(S, 0.01, 1e-200, matcher_algorithm="python")
    tsp = tsinfer.infer(S, positions, 0.01, 1e-200, matcher_algorithm="C")

    Sp = np.zeros((tsp.sample_size, tsp.num_sites), dtype="i1")
    for variant in tsp.variants():
        Sp[:, variant.index] = variant.genotypes
    assert np.all(Sp == S)
    # print(S)
    # print()
    # print(Sp)

    # for t in tsp.trees():
    #     print(t.interval, t)
    for site in tsp.sites():
        if len(site.mutations) > 1:
            print("Recurrent mutation")

    ts_simplified = tsp.simplify()
    # for h in ts_simplified.haplotypes():
    #     print(h)
    # for e in ts_simplified.edgesets():
    #     print(e.left, e.right, e.parent, e.children, sep="\t")
    # print()

    Sp = np.zeros((ts_simplified.sample_size, ts_simplified.num_sites), dtype="i1")
    for variant in ts_simplified.variants():
        Sp[:, variant.index] = variant.genotypes
    assert np.all(Sp == S)



def export_ancestors(n, L, seed):

    ts = msprime.simulate(
        n, length=L, recombination_rate=0.5, mutation_rate=1, random_seed=seed)
    print("num_sites = ", ts.num_sites)
    S = np.zeros((ts.sample_size, ts.num_sites), dtype="u1")
    for variant in ts.variants():
        S[:, variant.index] = variant.genotypes
    builder = tsinfer.AncestorBuilder(S)
    print("total ancestors = ", builder.num_ancestors)
    # matcher = tsinfer.AncestorMatcher(ts.num_sites)
    A = np.zeros((builder.num_ancestors, ts.num_sites), dtype=int)
    P = np.zeros((builder.num_ancestors, ts.num_sites), dtype=int)
    for j, a in enumerate(builder.build_all_ancestors()):
        # builder.build(j, A[j,:])
        A[j, :] = a
        if j % 100 == 0:
            print("done", j)
        # p = matcher.best_path(a, 0.01, 1e-200)
        # P[j,:] = p
        # matcher.add(a)
    # print("A = ")
    # print(A)
    # print("P = ")
    # print(P)
    np.savetxt("tmp__NOBACKUP__/ancestors.txt", A, fmt="%d", delimiter="\t")
    # np.savetxt("tmp__NOBACKUP__/path.txt", P, fmt="%d", delimiter="\t")

def export_samples(n, L, seed):

    ts = msprime.simulate(
        n, length=L, recombination_rate=0.5, mutation_rate=1, random_seed=seed)
    print("num_sites = ", ts.num_sites)
    with open("tmp__NOBACKUP__/samples.txt", "w") as out:
        for variant in ts.variants():
            print(variant.position, "".join(map(str, variant.genotypes)), sep="\t", file=out)




def compare_timings(n, L, seed):
    ts = msprime.simulate(
        n, length=L, recombination_rate=0.5, mutation_rate=1, random_seed=seed)
    if ts.num_sites == 0:
        print("zero sites; skipping")
        return
    S = np.zeros((ts.sample_size, ts.num_sites), dtype="i1")
    for variant in ts.variants():
        S[:, variant.index] = variant.genotypes

    total_matching_time_new = 0
    # Inline the code here so we can time it.
    samples = S
    num_samples, num_sites = samples.shape
    builder = tsinfer.AncestorBuilder(samples)
    matcher = _tsinfer.AncestorMatcher(num_sites)
    num_ancestors = builder.num_ancestors
    # tree_sequence_builder = TreeSequenceBuilder(num_samples, num_ancestors, num_sites)
    tree_sequence_builder = tsinfer.TreeSequenceBuilder(num_samples, num_ancestors, num_sites)

    A = np.zeros(num_sites, dtype=np.int8)
    P = np.zeros(num_sites, dtype=np.int32)
    M = np.zeros(num_sites, dtype=np.uint32)
    for j, A in enumerate(builder.build_all_ancestors()):
        before = time.clock()
        num_mutations = matcher.best_path(A, P, M, 0.01, 1e-200)
        total_matching_time_new += time.clock() - before
        # print(A)
        # print(P)
        # print("num_mutations = ", num_mutations, M[:num_mutations])
        assert num_mutations == 1
        # assert M[0] == focal_site
        matcher.add(A)
        tree_sequence_builder.add_path(j + 1, P, A, M[:num_mutations])
    # tree_sequence_builder.print_state()

    for j in range(num_samples):
        before = time.clock()
        num_mutations = matcher.best_path(samples[j], P, M, 0.01, 1e-200)
        total_matching_time_new += time.clock() - before
        u = num_ancestors + j + 1
        tree_sequence_builder.add_path(u, P, samples[j], M[:num_mutations])
    # tree_sequence_builder.print_state()
    tsp = tree_sequence_builder.finalise()

    Sp = np.zeros((tsp.sample_size, tsp.num_sites), dtype="i1")
    for variant in tsp.variants():
        Sp[:, variant.index] = variant.genotypes
    assert np.all(Sp == S)

    S = S.astype(np.uint8)
    panel = tsinfer.ReferencePanel(
        S, [site.position for site in ts.sites()], ts.sequence_length,
        rho=0.001, algorithm="c")
    before = time.clock()
    P, mutations = panel.infer_paths(num_workers=1)
    total_matching_time_old = time.clock() - before
    ts_new = panel.convert_records(P, mutations)
    Sp = np.zeros((ts_new.sample_size, ts_new.num_sites), dtype="u1")
    for variant in ts_new.variants():
        Sp[:, variant.index] = variant.genotypes
    assert np.all(Sp == S)
    print(n, L, total_matching_time_old, total_matching_time_new, sep="\t")

def ancestor_gap_density(n, L, seed):
    ts = msprime.simulate(
        n, length=L, recombination_rate=0.5, mutation_rate=1, random_seed=seed)
    if ts.num_sites == 0:
        print("zero sites; skipping")
        return
    S = np.zeros((ts.sample_size, ts.num_sites), dtype="i1")
    for variant in ts.variants():
        S[:, variant.index] = variant.genotypes

    samples = S
    num_samples, num_sites = samples.shape
    builder = tsinfer.AncestorBuilder(samples)
    # builder.print_state()
    matcher = tsinfer.AncestorMatcher(num_sites)
    num_ancestors = builder.num_ancestors

    # A = np.zeros(num_sites, dtype=np.int8)
    P = np.zeros(num_sites, dtype=np.int32)
    M = np.zeros(num_sites, dtype=np.uint32)
    for A in builder.build_all_ancestors():
        matcher.add(A)

#     for j in range(builder.num_ancestors):
#         focal_site = builder.site_order[j]
#         builder.build(j, A)
#         matcher.add(A)
    # matcher.print_state()
#     builder.print_state()
#     builder.print_all_ancestors()

    total_segments = np.zeros(ts.num_sites)
    total_blank_segments = np.zeros(ts.num_sites)
    total_blank_segments_distance = 0

    for l in range(matcher.num_sites):
        seg = matcher.sites_head[l]
        while seg is not None:
            # print(seg.start, seg.end, seg.value)
            total_segments[l] += 1
            if seg.value == -1:
                total_blank_segments[l] += 1
                total_blank_segments_distance += seg.end - seg.start
            seg = seg.next

    return {
        "n": n, "L": L,
        "num_sites":matcher.num_sites,
        "num_ancestors": matcher.num_ancestors,
        "mean_total_segments": np.mean(total_segments),
        "mean_blank_segments": np.mean(total_blank_segments),
        "total_blank_fraction": total_blank_segments_distance / (num_sites * num_ancestors)
    }

if __name__ == "__main__":

    np.set_printoptions(linewidth=20000)
    np.set_printoptions(threshold=200000)
    # main()
    # example()
    # bug()
    # for n in [100, 1000, 10000, 20000, 10**5]:
    #     ts_ls(n)
    # ts_ls(20)
    # leaf_lists_dev()

    # for m in [40]:
    #     segment_algorithm(100, m)
        # print()
    # segment_stats()
    # for j in range(1, 100000):
    #     print(j)
    #     new_segments(10, 100, j)
    # # new_segments(40, 10, 1)
    # new_segments(4, 4, 304)
    # export_ancestors(10, 500, 304)
    # export_samples(10, 10, 1)

    # n = 10
    # for L in np.linspace(100, 1000, 10):
    #     compare_timings(n, L, 1)

    # d = ancestor_gap_density(20, 40, 1)

    # rows = []
    # n = 10
    # for L in np.linspace(10, 5000, 20):
    #     d = ancestor_gap_density(n, L, 2)
    #     rows.append(d)
    #     df = pd.DataFrame(rows)
    #     print(df)
    #     df.to_csv("gap-analysis.csv")

    n = 10000
    for j in np.arange(1, 100, 10):
        print("n                :", n)
        print("L                :", j, "Mb")
        build_ancestors(n, j * 10**6, 1)
        print()

    # for j in range(1, 100000):
    #     build_ancestors(10, 10, j)
    #     if j % 1000 == 0:
    #         print(j)
