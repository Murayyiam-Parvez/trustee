import graphviz
import copy
import torch
import numpy as np
import pandas as pd

from enum import Enum

from sklearn import tree
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split
from sklearn.base import clone

from autogluon.tabular import TabularPredictor


# import prettytable
from prettytable import PrettyTable
from termcolor import colored

from skexplain.imitation import ClassificationDagger


class BlackboxVerdict(Enum):
    TRUSTWORTHY = 1, "green"
    INCONCLUSIVE = 2, "yellow"
    UNTRUSTWORTHY = 3, "red"

    def __new__(cls, *args, **kwds):
        obj = object.__new__(cls)
        obj._value_ = args[0]
        return obj

    def __init__(self, _: str, color: str = None):
        self._color_ = color

    def __str__(self):
        return self.value

    @property
    def color(self):
        return self._color_


def get_dt_info(dt):
    """Iterates through the given Decision Tree to collect relevant information for the trust report."""
    children_left = dt.tree_.children_left
    children_right = dt.tree_.children_right
    features = dt.tree_.feature
    thresholds = dt.tree_.threshold
    values = dt.tree_.value
    samples = dt.tree_.n_node_samples
    impurity = dt.tree_.impurity

    splits = []
    features_used = {}
    samples_sum = np.sum(samples)

    def walk_tree(node, path):
        """Recursively iterates through all nodes in given decision tree and returns them as a list."""
        if children_left[node] == children_right[node]:  # if leaf node
            node_class = np.argmax(values[node][0])
            return [
                {
                    "path": path,
                    "class": node_class,
                    "prob": (values[node][0][node_class] / np.sum(values[node][0])) * 100,
                    "samples": samples[node],
                }
            ]

        feature = features[node]
        threshold = thresholds[node]
        left = children_left[node]
        right = children_right[node]

        if feature not in features_used:
            features_used[feature] = {"count": 0, "samples": 0}

        features_used[feature]["count"] += 1
        features_used[feature]["samples"] += samples[node]

        splits.append(
            {
                "idx": node,
                "feature": feature,
                "threshold": threshold,
                "samples": samples[node],
                "values": values[node],
                "gini_split": (impurity[left], impurity[right]),
                "data_split": (np.sum(values[left]), np.sum(values[right])),
                "data_split_by_class": [(c_left, c_right) for (c_left, c_right) in zip(values[left][0], values[right][0])],
            }
        )

        return walk_tree(left, path + [(feature, "<=", threshold)]) + walk_tree(right, path + [(feature, ">", threshold)])

    branches = walk_tree(0, [])
    return (features_used, splits, branches, samples_sum)


def fit_and_explain(
    blackbox,
    X_train,
    y_train,
    X_test,
    y_test,
    dagger_num_iter=100,
    dagger_sample_size=0.5,
    class_names=None,
    logger=None,
    verbose=False,
):
    """
    Fits blacbox with the given X and y data, and uses Dagger to extract DT explanation
    """
    log = logger.log if logger else print

    # clone blackbox params but resets training weights to allow retraining with new dataset
    try:
        # scikit-learn models
        blackbox_copy = clone(blackbox)
    except Exception:
        # pytorch models
        blackbox_copy = copy.copy(blackbox)
        try:
            for layer in blackbox_copy.children():
                if hasattr(layer, "reset_parameters"):
                    layer.reset_parameters()
        except Exception as warn:
            print("warning", warn)
            # AutoGluon and any other models
            blackbox_copy = blackbox.__class__(blackbox._learner.label) if hasattr(blackbox, "_learner") else blackbox.__class__()

    if isinstance(blackbox, TabularPredictor):
        args = {}
        args[blackbox._learner.label] = y_train.values
        training_data = X_train.assign(**args)
        blackbox_copy.fit(training_data)
    else:
        blackbox_copy.fit(X_train, y_train)
    y_pred = blackbox_copy.predict(X_test)

    log("Blackbox model classification report with training data:")
    log("\n{}".format(classification_report(y_test, y_pred, digits=3)))

    # # Decision tree extraction
    log("Using Classification Dagger algorithm to extract DT...")
    dagger = ClassificationDagger(expert=blackbox_copy)

    dagger.fit(
        X_train,
        y_train,
        num_iter=dagger_num_iter,
        max_leaf_nodes=None,
        samples_size=dagger_sample_size,
        verbose=verbose,
    )

    log("#" * 10, "Explanation validation", "#" * 10)
    (dt, reward, idx) = dagger.explain()
    logger.log("Model explanation {} training fidelity: {}".format(idx, reward))
    dt_y_pred = dt.predict(X_test)

    log("Model explanation global fidelity report:")
    log(
        "\n{}".format(
            classification_report(y_pred, dt_y_pred, digits=3, labels=range(len(class_names)) if class_names else None, target_names=class_names)
        )
    )

    log("Model explanation classification report:")
    log(
        "\n{}".format(
            classification_report(y_test, dt_y_pred, digits=3, labels=range(len(class_names)) if class_names else None, target_names=class_names)
        )
    )
    log("#" * 10, "Done", "#" * 10)

    return (dt, y_pred, dt_y_pred)


def trust_report(
    blackbox,
    X=None,
    y=None,
    X_train=None,
    X_test=None,
    y_train=None,
    y_test=None,
    max_iter=10,
    train_size=0.7,
    dagger_num_iter=10,
    dagger_sample_size=0.3,
    top_n=10,
    logger=None,
    verbose=False,
    class_names=None,
    feature_names=None,
    output=None,
):
    """
    Builds trust report for given black-box model using the Dagger method to extract white-box explanations as Decision Trees.
    """
    log = logger.log if logger else print

    if (X is None and (X_train is None or X_test is None)) or (y is None and (y_train is None or y_test is None)):
        raise ValueError("Missing either X and y arguments or X_train, X_test, y_train and y_test arguments.")

    if X_train is None:
        # if data split is not given as a param, split the dataset randomly
        log("Splitting dataset for training and testing...")
        X_indexes = np.arange(0, X.shape[0])
        X_train, X_test, y_train, y_test = train_test_split(X_indexes, y, train_size=train_size)
        X_train = X.iloc[X_train]
        X_test = X.iloc[X_test]
        log("X size: {}; y size: {}".format(len(X), len(y)))
        log("Done!")

    dataset_size = len(X_train) + len(X_test)
    train_size = len(X_train) / dataset_size

    if feature_names is not None:
        feature_names = list(feature_names)
        feature_names = list(X_train.columns) if isinstance(X_train, pd.DataFrame) else feature_names

    ###############################################
    #               DATA COLLECTION               #
    ###############################################

    (first_dt, first_y_pred, first_dt_y_pred) = fit_and_explain(
        blackbox,
        X_train,
        y_train,
        X_test,
        y_test,
        dagger_num_iter=dagger_num_iter,
        dagger_sample_size=dagger_sample_size,
        class_names=class_names,
        verbose=verbose,
        logger=logger,
    )

    bb_class = type(blackbox).__name__
    bb_n_input_features = len(X_train.columns) if isinstance(X_train, pd.DataFrame) else len(X_train[0])
    bb_n_output_classes = len(np.unique(y_train))

    (first_dt_features, first_dt_nodes, first_dt_branches, first_dt_total_samples) = get_dt_info(first_dt)

    first_dt_class = type(first_dt).__name__
    first_dt_size = first_dt.tree_.node_count
    first_dt_samples = first_dt.tree_.n_node_samples[0]
    first_dt_samples_by_class = first_dt.tree_.value[0][0]

    first_dt_top_features = sorted(first_dt_features.items(), key=lambda p: p[1]["samples"], reverse=True)[:top_n]
    first_dt_top_nodes = sorted(first_dt_nodes, key=lambda p: p["samples"] * abs(p["gini_split"][0] - p["gini_split"][1]), reverse=True)[:top_n]
    first_dt_top_branches = sorted(first_dt_branches, key=lambda p: p["samples"], reverse=True)[:top_n]
    first_dt_n_features = len(first_dt_features.keys())
    first_dt_n_classes = first_dt.tree_.n_classes[0]

    it = 0
    whitebox_iter = []
    n_features_removed = 0
    top_feature_to_remove = first_dt_top_features[0][0]
    while it < max_iter and n_features_removed < bb_n_input_features:
        # remove most significant feature
        if isinstance(X_train, pd.DataFrame):
            X_train.iloc[:, top_feature_to_remove] = 0
            X_test.iloc[:, top_feature_to_remove] = 0
        elif isinstance(X_train, torch.Tensor):
            X_train[:, top_feature_to_remove] = torch.zeros(len(X_train))
            X_test[:, top_feature_to_remove] = torch.zeros(len(X_test))
        else:
            X_train[:, top_feature_to_remove] = np.zeros(len(X_train))
            X_test[:, top_feature_to_remove] = np.zeros(len(X_test))

        n_features_removed += 1

        (dt, y_pred, dt_y_pred) = fit_and_explain(
            blackbox,
            X_train,
            y_train,
            X_test,
            y_test,
            dagger_num_iter=dagger_num_iter,
            dagger_sample_size=dagger_sample_size,
            class_names=class_names,
            verbose=verbose,
            logger=logger,
        )

        whitebox_iter.append(
            {
                "it": it,
                "dt": dt,
                "y_pred": y_pred,
                "dt_y_pred": dt_y_pred,
                "feature_removed": top_feature_to_remove,
                "n_features_removed": n_features_removed,
                "f1": f1_score(y_test, y_pred, average="macro"),
                "classification_report": classification_report(
                    y_test, y_pred, digits=3, labels=range(len(class_names)) if class_names else None, target_names=class_names
                ),
                "fidelity": f1_score(y_pred, dt_y_pred, average="macro"),
                "fidelity_report": classification_report(
                    y_pred, dt_y_pred, digits=3, labels=range(len(class_names)) if class_names else None, target_names=class_names
                ),
            }
        )

        (iter_dt_features, _, _, _) = get_dt_info(dt)
        iter_dt_top_features = sorted(iter_dt_features.items(), key=lambda p: p[1]["samples"], reverse=True)[:top_n]
        top_feature_to_remove = iter_dt_top_features[0][0]
        it += 1

    ################################################
    #                    CHECKS                    #
    ################################################

    # bb_verdict_score = 100
    # if first_dt_n_features <= bb_n_input_features * 0.3:
    #     bb_verdict_score -= 10

    # if first_dt_n_classes < bb_n_output_classes:
    #     bb_verdict_score -= 10

    ################################################
    #                    REPORT                    #
    ################################################

    report = PrettyTable(title="Classification Trust Report", header=False)

    summary = PrettyTable(title="Summary")
    blackbox_report = PrettyTable(border=False, header=False)
    blackbox_report.align = "l"
    blackbox_report.add_row(["Model:", bb_class])
    blackbox_report.add_row(["Dataset size:", dataset_size])
    blackbox_report.add_row(["Train/Test Split:", "{:.2f}% / {:.2f}%".format(train_size * 100, (1 - train_size) * 100)])
    blackbox_report.add_row(["", ""])
    blackbox_report.add_row(["", ""])
    blackbox_report.add_row(["", ""])
    blackbox_report.add_row(["", ""])
    blackbox_report.add_row(["# Input features:", bb_n_input_features])
    blackbox_report.add_row(["# Output classes:", bb_n_output_classes])
    blackbox_report.add_row(["", ""])

    performance_report = PrettyTable(title="Performance", header=False)
    performance_report.add_column(
        "Performance",
        [classification_report(y_test, first_y_pred, digits=3, labels=range(len(class_names)) if class_names else None, target_names=class_names)],
    )

    summary.add_column("Blackbox", [blackbox_report, performance_report])

    whitebox_report = PrettyTable(border=False, header=False)
    whitebox_report.align = "l"
    whitebox_report.add_row(["Explanation method:", "Dagger"])
    whitebox_report.add_row(["Model:", first_dt_class])
    whitebox_report.add_row(["Iterations:", dagger_num_iter])
    whitebox_report.add_row(["Sample size:", "{:.2f}%".format(dagger_sample_size * 100)])
    whitebox_report.add_row(["", ""])
    whitebox_report.add_row(["Decision Tree Info", ""])
    whitebox_report.add_row(["Size:", first_dt_size])
    whitebox_report.add_row(["# Input features:", "{} ({:.2f}%)".format(first_dt_n_features, (first_dt_n_features / bb_n_input_features) * 100)])
    whitebox_report.add_row(["# Output classes:", "{} ({:.2f}%)".format(first_dt_n_classes, (first_dt_n_classes / bb_n_output_classes) * 100)])
    whitebox_report.add_row(["", ""])

    fidelity_report = PrettyTable(title="Fidelity", header=False)
    fidelity_report.add_column(
        "Fidelity",
        [
            classification_report(
                first_y_pred, first_dt_y_pred, digits=3, labels=range(len(class_names)) if class_names else None, target_names=class_names
            )
        ],
    )
    summary.add_column("Whitebox", [whitebox_report, fidelity_report])

    single_analysis = PrettyTable(title="Single-run Analysis", header=False)
    single_analysis_first_row = PrettyTable(header=False, border=False)

    top_features = PrettyTable(
        title="Top {} Features".format(len(first_dt_top_features)), field_names=["Feature", "# of Nodes (%)", "Data Split % - ↓"]
    )
    for (feat, values) in first_dt_top_features:
        top_features.add_row(
            [
                feature_names[feat] if feature_names else feat,
                "{} ({:.2f}%)".format(values["count"], (values["count"] / first_dt_size) * 100),
                "{:.2f}%".format((values["samples"] / first_dt_total_samples) * 100),
            ]
        )
        top_features.add_row(["", "", ""])

    top_nodes = PrettyTable(
        title="Top {} Nodes".format(len(first_dt_top_nodes)),
        field_names=["Decision", "Gini  Split - ↓", "Data Split % - ↓", "Data Split % by Class (L/R)"],
    )
    top_nodes.align = "l"
    top_nodes.valign = "m"

    for node in first_dt_top_nodes:
        top_nodes.add_row(
            [
                # node["idx"],
                "{} <= {}".format(feature_names[node["feature"]] if feature_names else node["feature"], node["threshold"]),
                "Left: {:.2f} \nRight: {:.2f}".format(node["gini_split"][0], node["gini_split"][1]),
                "Left: {:.2f}% \nRight: {:.2f}%".format(
                    (node["data_split"][0] / first_dt_samples) * 100, (node["data_split"][1] / first_dt_samples) * 100
                ),
                "\n".join(
                    [
                        "{}: {:.2f}% / {:.2f}%".format(
                            class_names[idx] if class_names and idx < len(class_names) else idx,
                            (count_left / first_dt_samples_by_class[idx]) * 100,
                            (count_right / first_dt_samples_by_class[idx]) * 100,
                        )
                        for idx, (count_left, count_right) in enumerate(node["data_split_by_class"])
                    ]
                ),
            ]
        )
        top_nodes.add_row(["", "", "", ""])

    top_branches = PrettyTable(title="Top {} Branches".format(top_n), field_names=["Rule", "Decision (P(x))", "Samples (%) - ↓", "Class Samples (%)"])
    top_branches.align = "l"
    top_branches.valign = "m"

    for branch in first_dt_top_branches:
        top_branches.add_row(
            [
                "\n and ".join(
                    ["{} {} {}".format(feature_names[feat] if feature_names else feat, op, threshold) for (feat, op, threshold) in branch["path"]]
                ),
                "{}\n({:.2f}%)".format(
                    class_names[branch["class"]] if class_names and branch["class"] < len(class_names) else branch["class"], branch["prob"]
                ),
                "{}\n({:.2f}%)".format(branch["samples"], (branch["samples"] / first_dt_samples) * 100),
                "({:.2f}%)".format((branch["samples"] / first_dt_samples_by_class[branch["class"]]) * 100),
            ]
        )
        top_branches.add_row(["", "", "", ""])

    single_analysis_first_row.add_column("Top Nodes", [top_nodes])
    single_analysis_first_row.add_column("Top Branches", [top_branches])

    single_analysis.add_column("Single Analysis", [top_features, single_analysis_first_row])

    repeated_analysis = PrettyTable(title="Repeated-run Analysis", header=False)
    iter_performance = PrettyTable(
        title="Iterative Feature Removal",
        field_names=["Iteration", "Feature Removed", "# Features Removed", "Performance", "Fidelity"],
    )
    iter_performance.align = "l"
    iter_performance.valign = "m"

    for iter in whitebox_iter:
        iter_performance.add_row(
            [
                iter["it"],
                feature_names[iter["feature_removed"]] if feature_names else iter["feature_removed"],
                iter["n_features_removed"],
                iter["classification_report"],
                iter["fidelity_report"],
            ]
        )
        iter_performance.add_row(["", "", "", "", ""])

    repeated_analysis.add_column("Iterative Feature Removal", [iter_performance])

    # if bb_verdict_score >= 80:
    #     bb_verdict = BlackboxVerdict.TRUSTWORTHY
    # elif bb_verdict_score >= 40:
    #     bb_verdict = BlackboxVerdict.INCONCLUSIVE
    # else:
    #     bb_verdict = BlackboxVerdict.UNTRUSTWORTHY

    # analysis = PrettyTable(title="Repeated-run Analysis", header=False)

    # verdict = PrettyTable(title="Verdict", header=False)
    # verdict.add_row(
    #     [
    #         "The blackbox model seems to be",
    #         "{} / {} / {}".format(
    #             colored(BlackboxVerdict.TRUSTWORTHY.name, BlackboxVerdict.TRUSTWORTHY.color),
    #             colored(BlackboxVerdict.INCONCLUSIVE.name, BlackboxVerdict.INCONCLUSIVE.color),
    #             colored(BlackboxVerdict.UNTRUSTWORTHY.name, BlackboxVerdict.UNTRUSTWORTHY.color),
    #         ),
    #     ]
    # )

    report.add_column("Report", [summary, single_analysis, repeated_analysis])

    log("\n{}".format(report))

    dot_data = tree.export_graphviz(
        first_dt,
        class_names=class_names,
        feature_names=feature_names,
        filled=True,
        rounded=True,
        special_characters=True,
    )

    if output:
        graph = graphviz.Source(dot_data)
        graph.render(output)