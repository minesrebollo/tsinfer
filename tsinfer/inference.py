#
# Copyright (C) 2018 University of Oxford
#
# This file is part of tsinfer.
#
# tsinfer is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# tsinfer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with tsinfer.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Central module for high-level inference. The actual implementation of
of the core tasks like ancestor generation and matching are delegated
to other modules.
"""
import collections
import queue
import time
import logging
import threading
import json

import numpy as np
import humanize
import msprime

import _tsinfer
import tsinfer.formats as formats
import tsinfer.algorithm as algorithm
import tsinfer.threads as threads
import tsinfer.provenance as provenance

logger = logging.getLogger(__name__)

UNKNOWN_ALLELE = 255
C_ENGINE = "C"
PY_ENGINE = "P"


class DummyProgress(object):
    """
    Class that mimics the subset of the tqdm API that we use in this module.
    """
    def update(self):
        pass

    def close(self):
        pass

    def set_postfix(self, *args, **kwargs):
        pass


class DummyProgressMonitor(object):
    """
    Simple class to mimic the interface of the real progress monitor.
    """
    def get(self, key, total):
        return DummyProgress()


def _get_progress_monitor(progress_monitor):
    if progress_monitor is None:
        progress_monitor = DummyProgressMonitor()
    return progress_monitor


def infer(
        sample_data, progress_monitor=None, num_threads=0, path_compression=True,
        engine=C_ENGINE):
    ancestor_data = generate_ancestors(
        sample_data, engine=engine, progress_monitor=progress_monitor)
    ancestors_ts = match_ancestors(
        sample_data, ancestor_data, engine=engine, num_threads=num_threads,
        path_compression=path_compression, progress_monitor=progress_monitor)
    inferred_ts = match_samples(
        sample_data, ancestors_ts, engine=engine, num_threads=num_threads,
        path_compression=path_compression, progress_monitor=progress_monitor)
    return inferred_ts


def generate_ancestors(sample_data, progress_monitor=None, engine=C_ENGINE, **kwargs):

    ancestor_data = formats.AncestorData.initialise(sample_data, **kwargs)
    progress_monitor = _get_progress_monitor(progress_monitor)
    num_sites = sample_data.num_inference_sites
    num_samples = sample_data.num_samples

    if engine == C_ENGINE:
        logger.debug("Using C AncestorBuilder implementation")
        ancestor_builder = _tsinfer.AncestorBuilder(num_samples, num_sites)
    elif engine == PY_ENGINE:
        logger.debug("Using Python AncestorBuilder implementation")
        ancestor_builder = algorithm.AncestorBuilder(num_samples, num_sites)
    else:
        raise ValueError("Unknown engine:{}".format(engine))

    progress = progress_monitor.get("ga_add_sites", num_sites)
    logger.info("Starting site addition")
    for j, (site_id, genotypes) in enumerate(
            sample_data.genotypes(inference_sites=True)):
        frequency = np.sum(genotypes)
        ancestor_builder.add_site(j, int(frequency), genotypes)
        progress.update()
    progress.close()
    logger.info("Finished adding sites")

    descriptors = ancestor_builder.ancestor_descriptors()
    progress = progress_monitor.get("ga_generate", len(descriptors))
    if len(descriptors) > 0:
        num_ancestors = len(descriptors)
        # Build the map from frequencies to time.
        time_map = {}
        for freq, _ in reversed(descriptors):
            if freq not in time_map:
                time_map[freq] = len(time_map) + 1
        logger.info("Starting build for {} ancestors".format(num_ancestors))
        a = np.zeros(num_sites, dtype=np.uint8)
        root_time = len(time_map) + 1
        ultimate_ancestor_time = root_time + 1
        # Add the ultimate ancestor. This is an awkward hack really; we don't
        # ever insert this ancestor. The only reason to add it here is that
        # it makes sure that the ancestor IDs we have in the ancestor file are
        # the same as in the ancestor tree sequence. This seems worthwhile.
        ancestor_data.add_ancestor(
            start=0, end=num_sites, time=ultimate_ancestor_time,
            focal_sites=[], haplotype=a)
        # Hack to ensure we always have a root with zeros at every position.
        ancestor_data.add_ancestor(
            start=0, end=num_sites, time=root_time,
            focal_sites=np.array([], dtype=np.int32), haplotype=a)
        for freq, focal_sites in descriptors:
            before = time.perf_counter()
            # TODO: This is a read-only process so we can multithread it.
            s, e = ancestor_builder.make_ancestor(focal_sites, a)
            assert np.all(a[s: e] != UNKNOWN_ALLELE)
            assert np.all(a[:s] == UNKNOWN_ALLELE)
            assert np.all(a[e:] == UNKNOWN_ALLELE)
            duration = time.perf_counter() - before
            logger.debug(
                "Made ancestor with {} focal sites and length={} in {:.2f}s.".format(
                    focal_sites.shape[0], e - s, duration))
            ancestor_data.add_ancestor(
                start=s, end=e, time=time_map[freq], focal_sites=focal_sites,
                haplotype=a)
            progress.update()
        progress.close()
    logger.info("Finished building ancestors")
    ancestor_data.finalise()
    return ancestor_data


def match_ancestors(
        sample_data, ancestor_data, progress_monitor=None, num_threads=0,
        path_compression=True, extended_checks=False, engine=C_ENGINE):
    """
    Runs the copying process of the specified input and ancestors and returns
    the resulting tree sequence.
    """
    matcher = AncestorMatcher(
        sample_data, ancestor_data, engine=engine,
        progress_monitor=progress_monitor, path_compression=path_compression,
        num_threads=num_threads, extended_checks=extended_checks)
    return matcher.match_ancestors()


def match_samples(
        sample_data, ancestors_ts, progress_monitor=None, num_threads=0,
        path_compression=True, simplify=True, extended_checks=False,
        stabilise_node_ordering=False, engine=C_ENGINE):
    manager = SampleMatcher(
        sample_data, ancestors_ts, path_compression=path_compression,
        engine=engine, progress_monitor=progress_monitor, num_threads=num_threads,
        extended_checks=extended_checks)
    manager.match_samples()
    ts = manager.finalise(
        simplify=simplify, stabilise_node_ordering=stabilise_node_ordering)
    return ts


class Matcher(object):

    def __init__(
            self, sample_data, num_threads=1, engine=C_ENGINE,
            path_compression=True, progress_monitor=None, extended_checks=False):
        self.sample_data = sample_data
        self.num_threads = num_threads
        self.path_compression = path_compression
        self.num_samples = self.sample_data.num_samples
        self.num_sites = self.sample_data.num_inference_sites
        self.progress_monitor = _get_progress_monitor(progress_monitor)
        self.match_progress = None  # Allocated by subclass
        self.extended_checks = extended_checks

        if engine == C_ENGINE:
            logger.debug("Using C matcher implementation")
            self.tree_sequence_builder_class = _tsinfer.TreeSequenceBuilder
            self.ancestor_matcher_class = _tsinfer.AncestorMatcher
        elif engine == PY_ENGINE:
            logger.debug("Using Python matcher implementation")
            self.tree_sequence_builder_class = algorithm.TreeSequenceBuilder
            self.ancestor_matcher_class = algorithm.AncestorMatcher
        else:
            raise ValueError("Unknown engine:{}".format(engine))
        self.tree_sequence_builder = None

        # Allocate 64K nodes and edges initially. This will double as needed and will
        # quickly be big enough even for very large instances.
        max_edges = 64 * 1024
        max_nodes = 64 * 1024
        self.tree_sequence_builder = self.tree_sequence_builder_class(
            num_sites=self.num_sites, max_nodes=max_nodes, max_edges=max_edges)
        logger.debug("Allocated tree sequence builder with max_nodes={}".format(
            max_nodes))

        # Allocate the matchers and statistics arrays.
        num_threads = max(1, self.num_threads)
        self.match = [np.zeros(self.num_sites, np.uint8) for _ in range(num_threads)]
        self.results = ResultBuffer()
        self.mean_traceback_size = np.zeros(num_threads)
        self.num_matches = np.zeros(num_threads)
        self.matcher = [
            self.ancestor_matcher_class(
                self.tree_sequence_builder, extended_checks=self.extended_checks)
            for _ in range(num_threads)]

    def _find_path(self, child_id, haplotype, start, end, thread_index=0):
        """
        Finds the path of the specified haplotype and upates the results
        for the specified thread_index.
        """
        matcher = self.matcher[thread_index]
        match = self.match[thread_index]
        # print("Find path", child_id)
        left, right, parent = matcher.find_path(haplotype, start, end, match)
        # print("Done")
        self.results.set_path(child_id, left, right, parent)
        self.match_progress.update()
        self.mean_traceback_size[thread_index] += matcher.mean_traceback_size
        self.num_matches[thread_index] += 1
        logger.debug("matched node {}; num_edges={} tb_size={:.2f} match_mem={}".format(
            child_id, left.shape[0], matcher.mean_traceback_size,
            humanize.naturalsize(matcher.total_memory, binary=True)))
        return left, right, parent

    def restore_tree_sequence_builder(self, ancestors_ts):
        # before = time.perf_counter()
        tables = ancestors_ts.dump_tables()
        nodes = tables.nodes
        self.tree_sequence_builder.restore_nodes(nodes.time, nodes.flags)
        edges = tables.edges
        # Need to sort by child ID here and left so that we can efficiently
        # insert the child paths.
        # TODO remove this step when we use a native zarr file for storing the
        # ancestor tree sequence. We output the edges in this order and we're
        # just sorting/resorting the edges here.
        index = np.lexsort((edges.left, edges.child))
        self.tree_sequence_builder.restore_edges(
            edges.left.astype(np.int32)[index],
            edges.right.astype(np.int32)[index],
            edges.parent[index],
            edges.child[index])
        mutations = tables.mutations
        self.tree_sequence_builder.restore_mutations(
            mutations.site, mutations.node, mutations.derived_state - ord('0'),
            mutations.parent)
        self.mutated_sites = mutations.site
        # print("SITE  =", self.mutated_sites)
        logger.info(
            "Loaded {} samples {} nodes; {} edges; {} sites; {} mutations".format(
                ancestors_ts.num_samples, len(nodes), len(edges), ancestors_ts.num_sites,
                len(mutations)))

    def get_ancestors_tree_sequence(self):
        """
        Return the ancestors tree sequence. In this tree sequence the coordinates
        are measured in units of site indexes and all ancestral and derived states
        are 0/1. All nodes have the sample flag bit set.
        """
        tsb = self.tree_sequence_builder
        flags, time = tsb.dump_nodes()
        nodes = msprime.NodeTable()
        nodes.set_columns(flags=flags, time=time)
        left, right, parent, child = tsb.dump_edges()
        position = np.arange(tsb.num_sites)
        sequence_length = max(1, tsb.num_sites)
        edges = msprime.EdgeTable()
        edges.set_columns(left=left, right=right, parent=parent, child=child)
        sites = msprime.SiteTable()
        sites.set_columns(
            position=position,
            ancestral_state=np.zeros(tsb.num_sites, dtype=np.int8) + ord('0'),
            ancestral_state_offset=np.arange(tsb.num_sites + 1, dtype=np.uint32))
        mutations = msprime.MutationTable()
        site, node, derived_state, parent = tsb.dump_mutations()
        derived_state += ord('0')
        mutations.set_columns(
            site=site, node=node, derived_state=derived_state,
            derived_state_offset=np.arange(tsb.num_mutations + 1, dtype=np.uint32),
            parent=parent)
        provenances = msprime.ProvenanceTable()
        for timestamp, record in self.sample_data.provenances():
            provenances.add_row(timestamp=timestamp, record=json.dumps(record))
        for timestamp, record in self.ancestor_data.provenances():
            provenances.add_row(timestamp=timestamp, record=json.dumps(record))
        record = provenance.get_provenance_dict(
            command="match-ancestors", source={"uuid": self.ancestor_data.uuid})
        provenances.add_row(record=json.dumps(record))
        msprime.sort_tables(nodes, edges, sites=sites, mutations=mutations)
        return msprime.load_tables(
            nodes=nodes, edges=edges, sites=sites, mutations=mutations,
            provenances=provenances, sequence_length=sequence_length)

    def encode_metadata(self, value):
        return json.dumps(value).encode()

    def locate_mutations_on_tree(self, tree, site, genotypes, alleles, mutations):
        """
        Find the most parsimonious way to place mutations to define the specified
        genotypes on the specified tree, and update the mutation table accordingly.
        """
        samples = np.where(genotypes == 1)[0]
        num_samples = len(samples)
        logger.debug("Locating mutations for site {}; n = {}".format(site, num_samples))
        # Nothing to do if this site is fixed for the ancestral state.
        if num_samples == 0:
            return

        count = np.zeros(tree.tree_sequence.num_nodes, dtype=int)
        for sample in samples:
            u = self.sample_ids[sample]
            while u != msprime.NULL_NODE:
                count[u] += 1
                u = tree.parent(u)
        # Go up the tree until we find the first node ancestral to all samples.
        mutation_node = self.sample_ids[samples[0]]
        while count[mutation_node] < num_samples:
            mutation_node = tree.parent(mutation_node)
        assert count[mutation_node] == num_samples

        parent_mutation = mutations.add_row(
            site=site, node=mutation_node, derived_state=alleles[1])
        # Traverse down the tree to find any leaves that do not have this
        # mutation and insert back mutations.
        for node in tree.nodes(mutation_node):
            if tree.is_leaf(node) and count[node] == 0:
                mutations.add_row(
                    site=site, node=node, derived_state=alleles[0],
                    parent=parent_mutation)

    def locate_mutations_over_samples(self, site, genotypes, alleles, mutations):
        """
        Place mutations directly over all the samples in the specified site.
        """
        for sample in np.where(genotypes == 1)[0]:
            node = self.sample_ids[sample]
            mutations.add_row(
                site=site, node=node, derived_state=alleles[1])

    def insert_sites(self, ts, sites, mutations):
        """
        Insert the sites in the sample data that were not marked for inference,
        updating the specified site and mutation tables. This is done by
        iterating over the trees
        """
        # progress_monitor = tqdm.tqdm(
        #     desc="place mutations", total=self.sample_data.num_sites,
        #     disable=not self.progress)
        num_sites = self.sample_data.num_sites
        progress_monitor = self.progress_monitor.get("ms_sites", num_sites)
        alleles = self.sample_data.sites_alleles[:]
        inference = self.sample_data.sites_inference[:]
        metadata = self.sample_data.sites_metadata[:]
        position = self.sample_data.sites_position[:]
        _, node, derived_state, parent = self.tree_sequence_builder.dump_mutations()
        inferred_site = 0
        trees = ts.trees()
        tree = next(trees)
        for site_id, genotypes in self.sample_data.genotypes():
            x = position[site_id]
            while tree.interval[1] <= x:
                tree = next(trees)
            assert tree.interval[0] <= x < tree.interval[1]
            sites.add_row(
                position=x,
                ancestral_state=alleles[site_id][0],
                metadata=self.encode_metadata(metadata[site_id]))
            if inference[site_id] == 1:
                mutations.add_row(
                    site=site_id, node=node[inferred_site],
                    derived_state=alleles[site_id][derived_state[inferred_site]])
                inferred_site += 1
            elif ts.num_edges > 0:
                self.locate_mutations_on_tree(
                    tree, site_id, genotypes, alleles[site_id], mutations)
            else:
                # If we have no tree topology this is all we can do.
                self.locate_mutations_over_samples(
                    site_id, genotypes, alleles[site_id], mutations)
            progress_monitor.update()
        progress_monitor.close()

    def get_samples_tree_sequence(self):
        """
        Returns the current state of the build tree sequence. All samples and
        ancestors will have the sample node flag set.
        """
        tsb = self.tree_sequence_builder
        nodes = msprime.NodeTable()
        flags, time = tsb.dump_nodes()

        logger.debug("Adding tree sequence nodes")
        # TODO add an option for encoding ancestor metadata in with the nodes here.
        # Add in the nodes up to for the ancestors.
        for u in range(self.sample_ids[0]):
            nodes.add_row(flags=flags[u], time=time[u])
        # Now add in the sample nodes with metadata, etc.
        sample_metadata = self.sample_data.samples_metadata[:]
        sample_population = self.sample_data.samples_population[:]
        for sample_id, metadata, population in zip(
                self.sample_ids, sample_metadata, sample_population):
            nodes.add_row(
                flags=flags[sample_id], time=time[sample_id],
                population=population,
                metadata=self.encode_metadata(metadata))
        # Add in the remaining synthetic nodes.
        for u in range(self.sample_ids[-1] + 1, tsb.num_nodes):
            nodes.add_row(flags=flags[u], time=time[u])

        logger.debug("Adding tree sequence edges")
        left, right, parent, child = tsb.dump_edges()
        inference_sites = self.sample_data.sites_inference[:]
        position = self.sample_data.sites_position[:]
        sequence_length = self.sample_data.sequence_length
        if sequence_length < position[-1]:
            sequence_length = position[-1] + 1

        # Subset down to the inference sites and map back to the site indexes.
        position = position[inference_sites == 1]
        pos_map = np.hstack([position, [sequence_length]])
        pos_map[0] = 0
        edges = msprime.EdgeTable()
        edges.set_columns(
            left=pos_map[left], right=pos_map[right], parent=parent, child=child)

        logger.debug("Sorting and building intermediate tree sequence.")
        msprime.sort_tables(nodes, edges)
        ts = msprime.load_tables(
            nodes=nodes, edges=edges, sequence_length=sequence_length)
        sites = msprime.SiteTable()
        mutations = msprime.MutationTable()
        self.insert_sites(ts, sites, mutations)

        provenances = msprime.ProvenanceTable()
        for prov in self.ancestors_ts.provenances():
            provenances.add_row(timestamp=prov.timestamp, record=prov.record)
        # We don't have a source here because tree sequence files don't have a
        # UUID yet.
        record = provenance.get_provenance_dict(command="match-samples")
        provenances.add_row(record=json.dumps(record))

        return msprime.load_tables(
            nodes=nodes, edges=edges, sites=sites, mutations=mutations,
            sequence_length=sequence_length, provenances=provenances)


class AncestorMatcher(Matcher):

    def __init__(self, sample_data, ancestor_data, **kwargs):
        super().__init__(sample_data, **kwargs)
        self.ancestor_data = ancestor_data
        self.num_ancestors = self.ancestor_data.num_ancestors
        self.epoch = self.ancestor_data.time[:]
        self.focal_sites = self.ancestor_data.focal_sites[:]
        self.start = self.ancestor_data.start[:]
        self.end = self.ancestor_data.end[:]
        self.match_progress = self.progress_monitor.get("ma_match", self.num_ancestors)

        # Create a list of all ID ranges in each epoch.
        if self.start.shape[0] == 0:
            self.num_epochs = 0
        else:
            breaks = np.where(self.epoch[1:] != self.epoch[:-1])[0]
            start = np.hstack([[0], breaks + 1])
            end = np.hstack([breaks + 1, [self.num_ancestors]])
            self.epoch_slices = np.vstack([start, end]).T
            self.num_epochs = self.epoch_slices.shape[0]
        self.start_epoch = 1
        # Add nodes for all the ancestors so that the ancestor IDs are equal
        # to the node IDs.
        for ancestor_id in range(self.num_ancestors):
            self.tree_sequence_builder.add_node(self.epoch[ancestor_id])

        self.ancestors = self.ancestor_data.ancestors()
        if self.num_epochs > 0:
            # Consume the first ancestor.
            a = next(self.ancestors)
            assert np.array_equal(a, np.zeros(self.num_sites, dtype=np.uint8))

    def __epoch_info_dict(self, epoch_index):
        start, end = self.epoch_slices[epoch_index]
        return collections.OrderedDict([
            ("epoch", str(self.epoch[start])),
            ("nanc", str(end - start))
        ])

    def __ancestor_find_path(self, ancestor_id, ancestor, thread_index=0):
        haplotype = np.zeros(self.num_sites, dtype=np.uint8) + UNKNOWN_ALLELE
        focal_sites = self.focal_sites[ancestor_id]
        start = self.start[ancestor_id]
        end = self.end[ancestor_id]
        self.results.set_mutations(ancestor_id, focal_sites)
        assert ancestor.shape[0] == (end - start)
        haplotype[start: end] = ancestor
        assert np.all(haplotype[focal_sites] == 1)
        logger.debug(
            "Finding path for ancestor {}; start={} end={} num_focal_sites={}".format(
                ancestor_id, start, end, focal_sites.shape[0]))
        haplotype[focal_sites] = 0
        left, right, parent = self._find_path(
                ancestor_id, haplotype, start, end, thread_index)
        assert np.all(self.match[thread_index][start: end] == haplotype[start: end])

    def __start_epoch(self, epoch_index):
        start, end = self.epoch_slices[epoch_index]
        info = collections.OrderedDict([
            ("epoch", str(self.epoch[start])),
            ("nanc", str(end - start))
        ])
        self.match_progress.set_postfix(info)
        self.tree_sequence_builder.freeze_indexes()

    def __complete_epoch(self, epoch_index):
        start, end = map(int, self.epoch_slices[epoch_index])
        num_ancestors_in_epoch = end - start
        current_time = self.epoch[start]
        nodes_before = self.tree_sequence_builder.num_nodes

        for child_id in range(start, end):
            left, right, parent = self.results.get_path(child_id)
            self.tree_sequence_builder.add_path(
                child_id, left, right, parent,
                compress=self.path_compression,
                extended_checks=self.extended_checks)
            site, derived_state = self.results.get_mutations(child_id)
            self.tree_sequence_builder.add_mutations(child_id, site, derived_state)

        extra_nodes = (
            self.tree_sequence_builder.num_nodes - nodes_before - num_ancestors_in_epoch)
        mean_memory = np.mean([matcher.total_memory for matcher in self.matcher])
        logger.debug(
            "Finished epoch {} with {} ancestors; {} extra nodes inserted; "
            "mean_tb_size={:.2f} edges={}; mean_matcher_mem={}".format(
                current_time, num_ancestors_in_epoch, extra_nodes,
                np.sum(self.mean_traceback_size) / np.sum(self.num_matches),
                self.tree_sequence_builder.num_edges,
                humanize.naturalsize(mean_memory, binary=True)))
        self.mean_traceback_size[:] = 0
        self.num_matches[:] = 0
        self.results.clear()

    def __match_ancestors_single_threaded(self):
        for j in range(self.start_epoch, self.num_epochs):
            self.__start_epoch(j)
            start, end = map(int, self.epoch_slices[j])
            for ancestor_id in range(start, end):
                a = next(self.ancestors)
                self.__ancestor_find_path(ancestor_id, a)
            self.__complete_epoch(j)

    def __match_ancestors_multi_threaded(self, start_epoch=1):
        # See note on match samples multithreaded below. Should combine these
        # into a single function. Possibly when trying to make the thread
        # error handling more robust.
        queue_depth = 8 * self.num_threads  # Seems like a reasonable limit
        match_queue = queue.Queue(queue_depth)

        def match_worker(thread_index):
            while True:
                work = match_queue.get()
                if work is None:
                    break
                ancestor_id, a = work
                self.__ancestor_find_path(ancestor_id, a, thread_index)
                match_queue.task_done()
            match_queue.task_done()

        match_threads = [
            threads.queue_consumer_thread(
                match_worker, match_queue, name="match-worker-{}".format(j),
                index=j)
            for j in range(self.num_threads)]
        logger.info("Started {} match worker threads".format(self.num_threads))

        for j in range(self.start_epoch, self.num_epochs):
            self.__start_epoch(j)
            start, end = map(int, self.epoch_slices[j])
            for ancestor_id in range(start, end):
                a = next(self.ancestors)
                match_queue.put((ancestor_id, a))
            # Block until all matches have completed.
            match_queue.join()
            self.__complete_epoch(j)

        # Stop the the worker threads.
        for j in range(self.num_threads):
            match_queue.put(None)
        for j in range(self.num_threads):
            match_threads[j].join()

    def match_ancestors(self):
        logger.info("Starting ancestor matching for {} epochs".format(self.num_epochs))
        if self.num_threads <= 0:
            self.__match_ancestors_single_threaded()
        else:
            self.__match_ancestors_multi_threaded()
        ts = self.store_output()
        logger.info("Finished ancestor matching")
        return ts

    def store_output(self):
        if self.num_ancestors > 0:
            ts = self.get_ancestors_tree_sequence()
        else:
            # Allocate an empty tree sequence.
            ts = msprime.load_tables(
                nodes=msprime.NodeTable(), edges=msprime.EdgeTable(), sequence_length=1)
        return ts


class SampleMatcher(Matcher):

    def __init__(self, sample_data, ancestors_ts, **kwargs):
        super().__init__(sample_data, **kwargs)
        self.restore_tree_sequence_builder(ancestors_ts)
        self.ancestors_ts = ancestors_ts
        self.sample_haplotypes = self.sample_data.haplotypes(inference_sites=True)
        self.sample_ids = np.zeros(self.num_samples, dtype=np.int32)
        for j in range(self.num_samples):
            self.sample_ids[j] = self.tree_sequence_builder.add_node(0)
        self.match_progress = self.progress_monitor.get("ms_match", self.num_samples)

    def __process_sample(self, sample_id, haplotype, thread_index=0):
        self._find_path(sample_id, haplotype, 0, self.num_sites, thread_index)
        match = self.match[thread_index]
        diffs = np.where(haplotype != match)[0]
        derived_state = haplotype[diffs]
        self.results.set_mutations(sample_id, diffs.astype(np.int32), derived_state)

    def __match_samples_single_threaded(self):
        j = 0
        for a in self.sample_haplotypes:
            sample_id = self.sample_ids[j]
            self.__process_sample(sample_id, a)
            j += 1
        assert j == self.num_samples

    def __match_samples_multi_threaded(self):
        # Note that this function is not almost identical to the match_ancestors
        # multithreaded function above. All we need to do is provide a function
        # to do the matching and some producer for the actual items and we
        # can bring this into a single function.

        queue_depth = 8 * self.num_threads  # Seems like a reasonable limit
        match_queue = queue.Queue(queue_depth)

        def match_worker(thread_index):
            while True:
                work = match_queue.get()
                if work is None:
                    break
                sample_id, a = work
                self.__process_sample(sample_id, a, thread_index)
                match_queue.task_done()
            match_queue.task_done()

        match_threads = [
            threads.queue_consumer_thread(
                match_worker, match_queue, name="match-worker-{}".format(j),
                index=j)
            for j in range(self.num_threads)]
        logger.info("Started {} match worker threads".format(self.num_threads))

        for sample_id, a in zip(self.sample_ids, self.sample_haplotypes):
            match_queue.put((sample_id, a))

        # Stop the the worker threads.
        for j in range(self.num_threads):
            match_queue.put(None)
        for j in range(self.num_threads):
            match_threads[j].join()

    def match_samples(self):
        logger.info("Started matching for {} samples".format(self.num_samples))
        if self.sample_data.num_inference_sites > 0:
            if self.num_threads <= 0:
                self.__match_samples_single_threaded()
            else:
                self.__match_samples_multi_threaded()
            self.match_progress.close()
            progress_monitor = self.progress_monitor.get("ms_paths", self.num_samples)
            for j in range(self.num_samples):
                sample_id = int(self.sample_ids[j])
                left, right, parent = self.results.get_path(sample_id)
                self.tree_sequence_builder.add_path(
                    sample_id, left, right, parent, compress=self.path_compression)
                site, derived_state = self.results.get_mutations(sample_id)
                self.tree_sequence_builder.add_mutations(sample_id, site, derived_state)
                progress_monitor.update()
            progress_monitor.close()
        logger.info("Finished sample matching")

    def finalise(self, simplify=True, stabilise_node_ordering=False):
        logger.info("Finalising tree sequence")
        ts = self.get_samples_tree_sequence()
        if simplify:
            logger.info("Running simplify on {} nodes and {} edges".format(
                ts.num_nodes, ts.num_edges))
            if stabilise_node_ordering:
                # Ensure all the node times are distinct so that they will have
                # stable IDs after simplifying. This could possibly also be done
                # by reversing the IDs within a time slice. This is used for comparing
                # tree sequences produced by perfect inference.
                tables = ts.tables
                time = tables.nodes.time
                for t in range(1, int(time[0])):
                    index = np.where(time == t)[0]
                    k = index.shape[0]
                    time[index] += np.arange(k)[::-1] / k
                tables.nodes.set_columns(flags=tables.nodes.flags, time=time)
                msprime.sort_tables(**tables.asdict())
                ts = msprime.load_tables(**tables.asdict())
            ts = ts.simplify(
                samples=self.sample_ids, filter_zero_mutation_sites=False)
            logger.info("Finished simplify; now have {} nodes and {} edges".format(
                ts.num_nodes, ts.num_edges))
        return ts


class ResultBuffer(object):
    """
    A wrapper for numpy arrays representing the results of a copying operations.
    """
    def __init__(self):
        self.paths = {}
        self.mutations = {}
        self.lock = threading.Lock()

    def clear(self):
        """
        Clears this result buffer.
        """
        self.paths.clear()
        self.mutations.clear()

    def set_path(self, node_id, left, right, parent):
        with self.lock:
            assert node_id not in self.paths
            self.paths[node_id] = left, right, parent

    def set_mutations(self, node_id, site, derived_state=None):
        if derived_state is None:
            derived_state = np.ones(site.shape[0], dtype=np.uint8)
        with self.lock:
            self.mutations[node_id] = site, derived_state

    def get_path(self, node_id):
        return self.paths[node_id]

    def get_mutations(self, node_id):
        return self.mutations[node_id]
